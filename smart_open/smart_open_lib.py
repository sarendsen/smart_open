#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (C) 2015 Radim Rehurek <me@radimrehurek.com>
#
# This code is distributed under the terms and conditions
# from the MIT License (MIT).


"""
Utilities for streaming from several file-like data storages: S3 / HDFS / standard
filesystem / compressed files..., using a single, Pythonic API.

The streaming makes heavy use of generators and pipes, to avoid loading
full file contents into memory, allowing work with arbitrarily large files.

The main methods are:

* `smart_open()`, which opens the given file for reading/writing
* `s3_iter_bucket()`, which goes over all keys in an S3 bucket in parallel

"""

import codecs
import collections
import logging
import os
import os.path as P
import sys
import requests
import importlib
import io
import warnings

# Import ``pathlib`` if the builtin ``pathlib`` or the backport ``pathlib2`` are
# available. The builtin ``pathlib`` will be imported with higher precedence.
for pathlib_module in ('pathlib', 'pathlib2'):
    try:
        pathlib = importlib.import_module(pathlib_module)
        PATHLIB_SUPPORT = True
        break
    except ImportError:
        PATHLIB_SUPPORT = False

from boto.compat import BytesIO, urlsplit, six
import boto.s3.key
import sys
from ssl import SSLError
from six.moves.urllib import parse as urlparse


IS_PY2 = (sys.version_info[0] == 2)

logger = logging.getLogger(__name__)

if IS_PY2:
    from bz2file import BZ2File
else:
    from bz2 import BZ2File

import gzip

#
# This module defines a function called smart_open so we cannot use
# smart_open.submodule to reference to the submodules.
#
import smart_open.s3 as smart_open_s3
from smart_open.s3 import iter_bucket as s3_iter_bucket
import smart_open.hdfs as smart_open_hdfs
import smart_open.webhdfs as smart_open_webhdfs
import smart_open.http as smart_open_http


SYSTEM_ENCODING = sys.getdefaultencoding()

_ISSUE_146_FSTR = (
    "You have explicitly specified encoding=%(encoding)s, but smart_open does "
    "not currently support decoding text via the %(scheme)s scheme. "
    "Re-open the file without specifying an encoding to suppress this warning."
)
_ISSUE_189_URL = 'https://github.com/RaRe-Technologies/smart_open/issues/189'

DEFAULT_ERRORS = 'strict'


Uri = collections.namedtuple(
    'Uri', 
    (
        'scheme',
        'uri_path',
        'bucket_id',
        'key_id',
        'port',
        'host',
        'ordinary_calling_format',
        'access_id',
        'access_secret',
    )
)
"""Represents all the options that we parse from user input.

Some of the above options only make sense for certain protocols, e.g.
bucket_id is only for S3.
"""
#
# Set the default values for all Uri fields to be None.  This allows us to only
# specify the relevant fields when constructing a Uri.
#
# https://stackoverflow.com/questions/11351032/namedtuple-and-default-values-for-optional-keyword-arguments
#
Uri.__new__.__defaults__ = (None,) * len(Uri._fields)


Kwargs = collections.namedtuple(
    'Kwargs',
    (
        'buffering',
        'encoding',
        'errors',
        'ignore_extension',
        'min_part_size',
        'kerberos',
        'user', 
        'password',
        'host',
        's3_min_part_size',
        's3_upload',
        's3_session',
        'profile_name',
    )
)
"""Collects all the keyword arguments that we support.

We pass these around often, so it's simpler to put them into a single namedtuple
instead of listing them explicitly, or using the **kwargs catch-all.
"""


def _capture_kwargs(**kwargs):
    filtered = {key: value for (key, value) in kwargs.items() if key in Kwargs._fields}
    return Kwargs(**filtered)


def smart_open(
        uri, mode="rb", buffering=-1, encoding=None, errors=DEFAULT_ERRORS,
        newline=None, closefd=True, opener=None,
        #
        # Parameters for built-in open function (Py3) above
        #
        ignore_extension=False,
        min_part_size=smart_open_webhdfs.WEBHDFS_MIN_PART_SIZE,
        kerberos=False,
        user=None,
        password=None,
        profile_name=None,
        host=None,
        s3_min_part_size=smart_open_s3.DEFAULT_MIN_PART_SIZE,
        s3_upload=None,
        s3_session=None,
        ):
    """
    Open the given S3 / HDFS / filesystem file pointed to by `uri` for reading or writing.

    The only supported modes for now are 'rb' (read, default) and 'wb' (replace & write).

    The reads/writes are memory efficient (streamed) and therefore suitable for
    arbitrarily large files.

    The `uri` can be either:

    1. a URI for the local filesystem (compressed ``.gz`` or ``.bz2`` files handled automatically):
       `./lines.txt`, `/home/joe/lines.txt.gz`, `file:///home/joe/lines.txt.bz2`
    2. a URI for HDFS: `hdfs:///some/path/lines.txt`
    3. a URI for Amazon's S3 (can also supply credentials inside the URI):
       `s3://my_bucket/lines.txt`, `s3://my_aws_key_id:key_secret@my_bucket/lines.txt`
    4. an instance of the boto.s3.key.Key class.
    5. an instance of the pathlib.Path class.

    Keyword parameters include all those from io.open, as well as some specific to smart_open.

    :param bool ignore_extension: If True, do not attempt to transparently compress/decompress.
    :param int min_part_size:
    :param bool kerberos: Whether to use Kerberos for authentication.
    :param str user: The HTTP username.
    :param str password: The HTTP password.
    :param int s3_min_part_size:
    :param dict s3_upload:
    :param boto3.Session s3_session:
    :param str profile_name: The boto3 profile name to use when connecting to S3.

    Some parameters affect certain modes only, e.g. kerberos only affects HTTP.

    Examples::

      >>> # stream lines from http; you can use context managers too:
      >>> with smart_open.smart_open('http://www.google.com') as fin:
      ...     for line in fin:
      ...         print line

      >>> # stream lines from S3; you can use context managers too:
      >>> with smart_open.smart_open('s3://mybucket/mykey.txt') as fin:
      ...     for line in fin:
      ...         print line

      >>> # you can also use a boto.s3.key.Key instance directly:
      >>> key = boto.connect_s3().get_bucket("my_bucket").get_key("my_key")
      >>> with smart_open.smart_open(key) as fin:
      ...     for line in fin:
      ...         print line

      >>> # stream line-by-line from an HDFS file
      >>> for line in smart_open.smart_open('hdfs:///user/hadoop/my_file.txt'):
      ...    print line

      >>> # stream content *into* S3:
      >>> with smart_open.smart_open('s3://mybucket/mykey.txt', 'wb') as fout:
      ...     for line in ['first line', 'second line', 'third line']:
      ...          fout.write(line + '\n')

      >>> # stream from/to (compressed) local files:
      >>> for line in smart_open.smart_open('/home/radim/my_file.txt'):
      ...    print line
      >>> for line in smart_open.smart_open('/home/radim/my_file.txt.gz'):
      ...    print line
      >>> with smart_open.smart_open('/home/radim/my_file.txt.gz', 'wb') as fout:
      ...    fout.write("hello world!\n")
      >>> with smart_open.smart_open('/home/radim/another.txt.bz2', 'wb') as fout:
      ...    fout.write("good bye!\n")
      >>> # stream from/to (compressed) local files with Expand ~ and ~user constructions:
      >>> for line in smart_open.smart_open('~/my_file.txt'):
      ...    print line
      >>> for line in smart_open.smart_open('my_file.txt'):
      ...    print line

    """
    logger.debug('%r', locals())
    kwargs = _capture_kwargs(**locals())

    if not isinstance(mode, six.string_types):
        raise TypeError('mode should be a string')

    fobj = _shortcut_open(uri, mode, kwargs)
    if fobj is not None:
        return fobj

    #
    # This is a work-around for the problem described in Issue #144.
    # If the user has explicitly specified an encoding, then assume they want
    # us to open the destination in text mode, instead of the default binary.
    #
    # If we change the default mode to be text, and match the normal behavior
    # of Py2 and 3, then the above assumption will be unnecessary.
    #
    if encoding is not None and 'b' in mode:
        mode = mode.replace('b', '')

    # Support opening ``pathlib.Path`` objects by casting them to strings.
    if PATHLIB_SUPPORT and isinstance(uri, pathlib.Path):
        uri = str(uri)

    #
    # This is how we get from the filename to the end result.  Decompression is
    # optional, but it always accepts bytes and returns bytes.
    #
    # Decoding is also optional, accepts bytes and returns text.  The diagram
    # below is for reading, for writing, the flow is from right to left, but
    # the code is identical.
    #
    #           open as binary         decompress?          decode?
    # filename ---------------> bytes -------------> bytes ---------> text
    #                          binary             decompressed       decode
    #
    try:
        binary_mode = {'r': 'rb', 'r+': 'rb+',
                       'w': 'wb', 'w+': 'wb+',
                       'a': 'ab', 'a+': 'ab+'}[mode]
    except KeyError:
        binary_mode = mode
    binary, filename = _open_binary_stream(uri, binary_mode, kwargs)
    if ignore_extension:
        decompressed = binary
    else:
        decompressed = _compression_wrapper(binary, filename, mode)

    actual_encoding = SYSTEM_ENCODING if kwargs.encoding is None else encoding
    if 'b' not in mode or encoding is not None:
        decoded = _encoding_wrapper(decompressed, mode, encoding=actual_encoding, errors=errors)
    else:
        decoded = decompressed

    return decoded


def _shortcut_open(uri, mode, kwargs):
    """Try to open the URI using the standard library io.open function.

    This can be much faster than the alternative of opening in binary mode and
    then decoding.

    This is only possible under the following conditions:

        1. Opening a local file
        2. Ignore extension is set to True

    If it is not possible to use the built-in open for the specified URI, returns None.

    :param str uri: A string indicating what to open.
    :param str mode: The mode to pass to the open function.
    :param Kwargs kwargs:
    :returns: The opened file
    :rtype: file
    """
    if not isinstance(uri, six.string_types):
        return None

    parsed_uri = _parse_uri(uri)
    if parsed_uri.scheme != 'file':
        return None

    _, extension = P.splitext(parsed_uri.uri_path)
    if extension in ('.gz', '.bz2') and not kwargs.ignore_extension:
        return None

    open_kwargs = {}
    if kwargs.errors is not None and 'b' not in mode:
        #
        # Ignore errors parameter if we're reading binary.  This logic is
        # necessary because:
        #
        #   - open complains if you pass non-null errors in binary mode
        #   - our default mode is rb (incompatible with default Python open)
        #   - our default errors is non-null (to match default Python open)
        #
        open_kwargs['errors'] = kwargs.errors

    if kwargs.encoding is not None:
        open_kwargs['encoding'] = kwargs.encoding
        mode = mode.replace('b', '')

    #
    # Under Py3, the built-in open accepts kwargs, and it's OK to use that.
    # Under Py2, the built-in open _doesn't_ accept kwargs, but we still use it
    # whenever possible (see issue #207).  If we're under Py2 and have to use
    # kwargs, then we have no option other to use io.open.
    #
    if six.PY3:
        return open(parsed_uri.uri_path, mode, buffering=kwargs.buffering, **open_kwargs)
    elif not open_kwargs:
        return open(parsed_uri.uri_path, mode, buffering=kwargs.buffering)
    return io.open(parsed_uri.uri_path, mode, buffering=kwargs.buffering, **open_kwargs)


def _open_binary_stream(uri, mode, kwargs):
    """Open an arbitrary URI in the specified binary mode.

    Not all modes are supported for all protocols.

    :arg uri: The URI to open.  May be a string, or something else.
    :arg str mode: The mode to open with.  Must be rb, wb or ab.
    :param Kwargs kwargs:

    :returns: A file object and the filename
    :rtype: tuple
    """
    if mode not in ('rb', 'rb+', 'wb', 'wb+', 'ab', 'ab+'):
        #
        # This should really be a ValueError, but for the sake of compatibility
        # with older versions, which raise NotImplementedError, we do the same.
        #
        raise NotImplementedError('unsupported mode: %r' % mode)

    if isinstance(uri, six.string_types):
        # this method just routes the request to classes handling the specific storage
        # schemes, depending on the URI protocol in `uri`
        filename = uri.split('/')[-1]
        parsed_uri = _parse_uri(uri)
        unsupported = "%r mode not supported for %r scheme" % (mode, parsed_uri.scheme)

        if parsed_uri.scheme in ("file", ):
            # local files -- both read & write supported
            # compression, if any, is determined by the filename extension (.gz, .bz2)
            fobj = io.open(parsed_uri.uri_path, mode)
            return fobj, filename
        elif parsed_uri.scheme in ("s3", "s3n", 's3u'):
            endpoint_url = None if kwargs.host is None else 'http://' + kwargs.host

            #
            # TODO: this shouldn't be smart_open's responsibility.
            #
            if kwargs.s3_session is None:
                import boto3
                session = boto3.Session(
                    profile_name=kwargs.profile_name,
                    aws_access_key_id=parsed_uri.access_id,
                    aws_secret_access_key=parsed_uri.access_secret,
                )
                resource = session.resource('s3', endpoint_url=endpoint_url)
            else:
                assert parsed_uri.access_id is None, 'aws_access_key_id conflicts with session'
                assert parsed_uri.access_secret is None, 'aws_secret_access_key conflicts with session'
                resource = kwargs.s3_session.resource('s3', endpoint_url=endpoint_url)

            fobj = smart_open_s3.open(
                parsed_uri.bucket_id,
                parsed_uri.key_id,
                mode,
                resource=resource,
                min_part_size=kwargs.s3_min_part_size,
                multipart_upload_kwargs=kwargs.s3_upload,
            )
            return fobj, filename
        elif parsed_uri.scheme in ("hdfs", ):
            if mode == 'rb':
                return smart_open_hdfs.CliRawInputBase(parsed_uri.uri_path), filename
            elif mode == 'wb':
                return smart_open_hdfs.CliRawOutputBase(parsed_uri.uri_path), filename
            else:
                raise NotImplementedError(unsupported)
        elif parsed_uri.scheme in ("webhdfs", ):
            if mode == 'rb':
                fobj = smart_open_webhdfs.BufferedInputBase(parsed_uri.uri_path)
            elif mode == 'wb':
                fobj = smart_open_webhdfs.BufferedOutputBase(
                    parsed_uri.uri_path, min_part_size=kwargs.min_part_size
                )
            else:
                raise NotImplementedError(unsupported)
            return fobj, filename
        elif parsed_uri.scheme.startswith('http'):
            #
            # The URI may contain a query string and fragments, which interfere
            # with out compressed/uncompressed estimation.
            #
            filename = P.basename(urlparse.urlparse(uri).path)
            if mode == 'rb':
                fobj = smart_open_http.BufferedInputBase(
                    uri, kerberos=kwargs.kerberos,
                    user=kwargs.user, password=kwargs.password
                )
                return fobj, filename
            else:
                raise NotImplementedError(unsupported)
        else:
            raise NotImplementedError("scheme %r is not supported", parsed_uri.scheme)
    elif isinstance(uri, boto.s3.key.Key):
        logger.debug('%r', locals())
        endpoint_url = None if args.host is None else 'http://' + args.host
        fobj = smart_open_s3.open(
            uri.bucket.name, uri.name, mode, endpoint_url=endpoint_url,
            min_part_size=kwargs.min_part_size, profile_name=kwargs.profile_name,
        )
        return fobj, uri.name
    elif hasattr(uri, 'read'):
        # simply pass-through if already a file-like
        filename = '/tmp/unknown'
        return uri, filename
    else:
        raise TypeError('don\'t know how to handle uri %s' % repr(uri))


def _parse_uri(uri_as_string):
    """
    Parse the given URI from a string.

    Supported URI schemes are "file", "s3", "s3n", "s3u" and "hdfs".

      * s3 and s3n are treated the same way.
      * s3u is s3 but without SSL.

    Valid URI examples::

      * s3://my_bucket/my_key
      * s3://my_key:my_secret@my_bucket/my_key
      * s3://my_key:my_secret@my_server:my_port@my_bucket/my_key
      * hdfs:///path/file
      * hdfs://path/file
      * webhdfs://host:port/path/file
      * ./local/path/file
      * ~/local/path/file
      * local/path/file
      * ./local/path/file.gz
      * file:///home/user/file
      * file:///home/user/file.bz2
    """
    if os.name == 'nt':
        # urlsplit doesn't work on Windows -- it parses the drive as the scheme...
        if '://' not in uri_as_string:
            # no protocol given => assume a local file
            uri_as_string = 'file://' + uri_as_string
    parsed_uri = urlsplit(uri_as_string, allow_fragments=False)

    if parsed_uri.scheme == "hdfs":
        return _parse_uri_hdfs(parsed_uri)
    elif parsed_uri.scheme == "webhdfs":
        return _parse_uri_webhdfs(parsed_uri)
    elif parsed_uri.scheme in ("s3", "s3n", "s3u"):
        return _parse_uri_s3x(parsed_uri)
    elif parsed_uri.scheme in ('file', '', None):
        return _parse_uri_file(parsed_uri)
    elif parsed_uri.scheme.startswith('http'):
        return Uri(scheme=parsed_uri.scheme, uri_path=uri_as_string)
    else:
        raise NotImplementedError(
            "unknown URI scheme %r in %r" % (parsed_uri.scheme, uri_as_string)
        )


def _parse_uri_hdfs(parsed_uri):
    assert parsed_uri.scheme == 'hdfs'
    uri_path = parsed_uri.netloc + parsed_uri.path
    uri_path = "/" + uri_path.lstrip("/")
    if not uri_path:
        raise RuntimeError("invalid HDFS URI: %s" % str(parsed_uri))

    return Uri(scheme='hdfs', uri_path=uri_path)


def _parse_uri_webhdfs(parsed_uri):
    assert parsed_uri.scheme == 'webhdfs'
    uri_path = parsed_uri.netloc + "/webhdfs/v1" + parsed_uri.path
    if parsed_uri.query:
        uri_path += "?" + parsed_uri.query
    if not uri_path:
        raise RuntimeError("invalid WebHDFS URI: %s" % str(parsed_uri))

    return Uri(scheme='webhdfs', uri_path=uri_path)


def _parse_uri_s3x(parsed_uri):
    assert parsed_uri.scheme in ("s3", "s3n", "s3u")

    port = 443
    host = boto.config.get('s3', 'host', 's3.amazonaws.com')
    ordinary_calling_format = False

    # Common URI template [secret:key@][host[:port]@]bucket/object
    try:
        uri = parsed_uri.netloc + parsed_uri.path
        # Separate authentication from URI if exist
        if ':' in uri.split('@')[0]:
            auth, uri = uri.split('@', 1)
            access_id, access_secret = auth.split(':')
        else:
            # "None" credentials are interpreted as "look for credentials in other locations" by boto
            access_id, access_secret = None, None

        # Split [host[:port]@]bucket/path
        host_bucket, key_id = uri.split('/', 1)
        if '@' in host_bucket:
            host_port, bucket_id = host_bucket.split('@')
            ordinary_calling_format = True
            if ':' in host_port:
                server = host_port.split(':')
                host = server[0]
                if len(server) == 2:
                    port = int(server[1])
            else:
                host = host_port
        else:
            bucket_id = host_bucket
    except Exception:
        # Bucket names must be at least 3 and no more than 63 characters long.
        # Bucket names must be a series of one or more labels.
        # Adjacent labels are separated by a single period (.).
        # Bucket names can contain lowercase letters, numbers, and hyphens.
        # Each label must start and end with a lowercase letter or a number.
        raise RuntimeError("invalid S3 URI: %s" % str(parsed_uri))

    return Uri(
        scheme=parsed_uri.scheme, bucket_id=bucket_id, key_id=key_id,
        port=port, host=host, ordinary_calling_format=ordinary_calling_format,
        access_id=access_id, access_secret=access_secret
    )


def _parse_uri_file(parsed_uri):
    assert parsed_uri.scheme in (None, '', 'file')
    uri_path = parsed_uri.netloc + parsed_uri.path
    # '~/tmp' may be expanded to '/Users/username/tmp'
    uri_path = os.path.expanduser(uri_path)

    if not uri_path:
        raise RuntimeError("invalid file URI: %s" % str(parsed_uri))

    return Uri(scheme='file', uri_path=uri_path)


def _need_to_buffer(file_obj, mode, ext):
    """Returns True if we need to buffer the whole file in memory in order to proceed."""
    try:
        is_seekable = file_obj.seekable()
    except AttributeError:
        #
        # Under Py2, built-in file objects returned by open do not have
        # .seekable, but have a .seek method instead.
        #
        is_seekable = hasattr(file_obj, 'seek')
    return six.PY2 and mode.startswith('r') and ext in ('.gz', '.bz2') and not is_seekable


def _compression_wrapper(file_obj, filename, mode):
    """
    This function will wrap the file_obj with an appropriate
    [de]compression mechanism based on the extension of the filename.

    file_obj must either be a filehandle object, or a class which behaves
        like one.

    If the filename extension isn't recognized, will simply return the original
    file_obj.
    """
    _, ext = os.path.splitext(filename)

    if _need_to_buffer(file_obj, mode, ext):
        warnings.warn('streaming gzip support unavailable, see %s' % _ISSUE_189_URL)
        file_obj = io.BytesIO(file_obj.read())

    if ext == '.bz2':
        return BZ2File(file_obj, mode)
    elif ext == '.gz':
        return gzip.GzipFile(fileobj=file_obj, mode=mode)
    else:
        return file_obj


def _encoding_wrapper(fileobj, mode, encoding=SYSTEM_ENCODING, errors=DEFAULT_ERRORS):
    """Decode bytes into text, if necessary.

    If mode specifies binary access, does nothing, unless the encoding is
    specified.  A non-null encoding implies text mode.

    :arg fileobj: must quack like a filehandle object.
    :arg str mode: is the mode which was originally requested by the user.
    :arg str encoding: The text encoding to use.  If mode is binary, overrides mode.
    :arg str errors: The method to use when handling encoding/decoding errors.
    :returns: a file object
    """
    logger.debug('encoding_wrapper: %r', locals())

    #
    # If the mode is binary, but the user specified an encoding, assume they
    # want text.  If we don't make this assumption, ignore the encoding and
    # return bytes, smart_open behavior will diverge from the built-in open:
    #
    #   open(filename, encoding='utf-8') returns a text stream in Py3
    #   smart_open(filename, encoding='utf-8') would return a byte stream
    #       without our assumption, because the default mode is rb.
    #
    if 'b' in mode and encoding is None:
        return fileobj
    if mode[0] == 'r':
        decoder = codecs.getreader(encoding)
    else:
        decoder = codecs.getwriter(encoding)
    return decoder(fileobj, errors=errors)
