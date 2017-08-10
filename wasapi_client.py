#!/usr/bin/env python

import argparse
import getpass
import logging
import logging.handlers
import math
import multiprocessing
import os
import requests
import sys
from json.decoder import JSONDecodeError
from queue import Empty
from urllib.parse import urlencode


NAME = 'wasapi-client' if __name__ == '__main__' else __name__
MAIN_LOGGER = logging.getLogger('main')


def do_listener_logging(log_q, path=''):
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    if path:
        handler = logging.FileHandler(filename=path)
    else:
        handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    # Get records from the queue and send them to the handler.
    listener = logging.handlers.QueueListener(log_q, handler)
    listener.start()

    # Add the handler to the logger, so records from this process are written.
    logger = logging.getLogger(NAME)
    logger.addHandler(handler)
    return listener


def configure_worker_logging(log_q, log_level=logging.ERROR, logger_name=None):
    logger = logging.getLogger(logger_name)
    logger.setLevel(log_level)
    logger.addHandler(logging.handlers.QueueHandler(log_q))


class WASAPIDownloadError(Exception):
    pass


def make_session(auth=None):
    """Make a session that will store our auth.

    `auth` is a tuple of the form (user, password)
    """
    session = requests.Session()
    session.auth = auth
    return session


def get_webdata(webdata_uri, session):
    """Make a request to the WASAPI."""
    try:
        response = session.get(webdata_uri)
    except requests.exceptions.ConnectionError as err:
        sys.exit('Could not connect at {}:\n{}'.format(webdata_uri, err))
    MAIN_LOGGER.debug('requesting {}'.format(webdata_uri))
    if response.status_code == 403:
        sys.exit('Verify user/password for {}:\n{} {}'.format(webdata_uri,
                                                              response.status_code,
                                                              response.reason))
    try:
        return response.json()
    except JSONDecodeError as err:
        sys.exit('Non-JSON response from {}'.format(webdata_uri))


def populate_downloads(page_uri, auth=None):
    """Repeat webdata requests to gather downloadable file info.

    Returns a queue containing file locations and checksums.
    """
    session = make_session(auth)
    get_q = multiprocessing.JoinableQueue()
    while page_uri:
        webdata = get_webdata(page_uri, session)
        for f in webdata['files']:
            get_q.put({'locations': f['locations'],
                       'filename': f['filename'],
                       'checksums': f['checksums']})
        page_uri = webdata.get('next', None)
    session.close()
    return get_q


def get_files_count(webdata_uri, auth=None):
    """Return total number of downloadable files."""
    session = make_session(auth)
    webdata = get_webdata(webdata_uri, session)
    session.close()
    return webdata.get('count', None)


def get_files_size(page_uri, auth=None):
    """Return total size (bytes) of downloadable files."""
    session = make_session(auth)
    total = 0
    count = 0
    webdata = None
    while page_uri:
        webdata = get_webdata(page_uri, session)
        for f in webdata['files']:
            total += int(f['size'])
        page_uri = webdata.get('next', None)
    if webdata:
        count = webdata.get('count', None)
    session.close()
    return count, total


def convert_bytes(size):
    """Make a human readable size."""
    label = ('B', 'KB', 'MB', 'GB', 'TB', 'PB', 'EB', 'ZB', 'YB')
    try:
        i = int(math.floor(math.log(size, 1024)))
    except ValueError:
        i = 0
    p = math.pow(1024, i)
    readable_size = round(size/p, 2)
    return '{}{}'.format(readable_size, label[i])


def download_file(file_data, session, destination=''):
    """Download webdata file to disk."""
    for location in file_data['locations']:
        response = session.get(location, stream=True)
        msg = '{}: {} {}'.format(location,
                                 response.status_code,
                                 response.reason)
        if response.status_code == 200:
            output_path = os.path.join(destination, file_data['filename'])
            try:
                write_file(response, output_path)
            except OSError as err:
                logging.error('{}: {}'.format(location, str(err)))
                break
            # Successful download; don't try alternate locations.
            logging.info(msg)
            return msg
        else:
            logging.error(msg)
    # We didn't download successfully; raise error.
    msg = 'FAILED to download {} from {}'.format(file_data['filename'],
                                                 file_data['locations'])
    raise WASAPIDownloadError(msg)


def write_file(response, output_path=''):
    """Write file to disk."""
    with open(output_path, 'wb') as wtf:
        for chunk in response.iter_content(1024*4):
            wtf.write(chunk)


class Downloader(multiprocessing.Process):
    """Worker for downloading web files with a persistent session."""

    def __init__(self, get_q, result_q, log_q, log_level=logging.ERROR,
                 auth=None, destination='.', *args, **kwargs):
        super(Downloader, self).__init__(*args, **kwargs)
        self.get_q = get_q
        self.result_q = result_q
        self.session = make_session(auth)
        self.destination = destination
        configure_worker_logging(log_q, log_level)

    def run(self):
        """Download files from the queue until there are no more.

        Gets a file's data off the queue, attempts to download the
        the file, and puts the result onto another queue.

        A get_q item looks like:
         {'locations': ['http://...', 'http://...'],
          'filename': 'blah.warc.gz',
          'checksums': {'sha1': '33304d104f95d826da40079bad2400dc4d005403',
                        'md5': '62f87a969af0dd857ecd6c3e7fde6aed'}}
        """
        while True:
            try:
                file_data = self.get_q.get(block=False)
            except Empty:
                break
            try:
                result = download_file(file_data, self.session, self.destination)
            except WASAPIDownloadError as err:
                logging.error(str(err))
                result = str(err)  # TO DO: figure out what this should be
            # TO DO: ADD checksum verification
            self.result_q.put(result)
            self.get_q.task_done()


class SetQueryParametersAction(argparse.Action):
    """Store all of the query parameter argument values in a dict."""

    def __call__(self, parser, namespace, values, option_string):
        if not hasattr(namespace, 'query_params'):
            setattr(namespace, 'query_params', {})
        option = option_string.lstrip('-')
        namespace.query_params[option] = values


def _build_parser():
    """Parse the commandline arguments."""
    description = """
        Download WARC files from a WASAPI access point.

        Acceptable date/time formats are:
         2017-01-01
         2017-01-01T12:34:56
         2017-01-01 12:34:56
         2017-01-01T12:34:56Z
         2017-01-01 12:34:56-0700
         2017
         2017-01"""
    parser = argparse.ArgumentParser(description=description,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)

    parser.add_argument('-u',
                        '--user',
                        dest='user',
                        help='username for API authentication')
    parser.add_argument('-b',
                        '--base-uri',
                        dest='base_uri',
                        default='https://partner.archive-it.org/wasapi/v1/webdata',
                        help='base URI for WASAPI access; default: '
                             'https://partner.archive-it.org/wasapi/v1/webdata')
    parser.add_argument('-d',
                        '--destination',
                        default='.',
                        help='location for storing downloaded files')
    parser.add_argument('--log',
                        help='file to which logging should be written')
    parser.add_argument('-v',
                        '--verbose',
                        action='count',
                        default=0,
                        help='log verbosely; -v is INFO, -vv is DEBUG')

    out_group = parser.add_mutually_exclusive_group()
#    out_group.add_argument('-l',
#                       '--list',
#                       action='store_true',
#                       help='list files with checksums and exit')
    out_group.add_argument('-c',
                           '--count',
                           action='store_true',
                           help='print number of files for download and exit')
    out_group.add_argument('-p',
                           '--processes',
                           type=int,
                           default=multiprocessing.cpu_count(),
                           help='number of WARC downloading processes')
    out_group.add_argument('-s',
                           '--size',
                           action='store_true',
                           help='print count and total size of files and exit')

    # Arguments to become part of query parameter string
    param_group = parser.add_argument_group('query parameters',
                                            'parameters for webdata request')
    param_group.add_argument('--collection',
                             action=SetQueryParametersAction,
                             nargs='+',
                             help='collection identifier')
    param_group.add_argument('--filename',
                             action=SetQueryParametersAction,
                             help='exact webdata filename to download')
    param_group.add_argument('--crawl',
                             action=SetQueryParametersAction,
                             help='crawl job identifier')
    param_group.add_argument('--crawl-time-after',
                             action=SetQueryParametersAction,
                             help='request files with date of creation '
                                  'during a crawl job after this date')
    param_group.add_argument('--crawl-time-before',
                             action=SetQueryParametersAction,
                             help='request files with date of creation '
                                  'during a crawl job before this date')
    param_group.add_argument('--crawl-start-after',
                             action=SetQueryParametersAction,
                             help='request files from crawl jobs starting '
                                  'after this date')
    param_group.add_argument('--crawl-start-before',
                             action=SetQueryParametersAction,
                             help='request files from crawl jobs starting '
                                  'before this date')
    return parser


def main():
    parser = _build_parser()
    args = parser.parse_args()

    if not os.access(args.destination, os.W_OK):
        msg = 'Cannot write to destination: {}'.format(args.destination)
        sys.exit(msg)

    # Start log writing process.
    log_q = multiprocessing.Queue()
    try:
        listener = do_listener_logging(log_q, args.log)
    except OSError as err:
        print('Could not open file for logging:', err)
        sys.exit(1)

    # Configure a logger for the main process.
    try:
        log_level = [logging.ERROR, logging.INFO, logging.DEBUG][args.verbose]
    except IndexError:
        log_level = logging.DEBUG
    configure_worker_logging(log_q, log_level, 'main')

    # Generate query string for the webdata request.
    try:
        query = '?{}'.format(urlencode(args.query_params, safe=':', doseq=True))
    except AttributeError:
        query = ''
    webdata_uri = '{}{}'.format(args.base_uri, query)

    # Generate authentication tuple for the API calls.
    auth = None
    if args.user:
        auth = (args.user, getpass.getpass())

    # If user wants the size, don't download files.
    if args.size:
        count, size = get_files_size(webdata_uri, auth)
        print('Number of Files: ', count)
        print('Size of Files: ', convert_bytes(size))
        sys.exit()

    # If user wants a count, don't download files.
    if args.count:
        print('Number of Files: ', get_files_count(webdata_uri, auth))
        sys.exit()

    # Process webdata requests to fill webdata file queue.
    # Then start downloading with multiple processes.
    get_q = populate_downloads(webdata_uri, auth)
    result_q = multiprocessing.Queue()
    for _ in range(args.processes):
        Downloader(get_q, result_q, log_q, log_level, auth, args.destination).start()
    get_q.join()

    listener.stop()

    result = []
    while not result_q.empty():
        result.append(result_q.get())
        # need to notify about bad checksum
    print(result)


if __name__ == '__main__':
    main()
