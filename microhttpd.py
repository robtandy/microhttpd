# -*- coding: utf-8 -*-
__author__ = 'Rob Tandy'
__license__ = 'MIT'
__version__ = '0.5.0'

import urllib.parse
import re
import os
import inspect
import logging
import asyncio
import time
from asyncio import coroutine, Task, start_server

log = logging.getLogger('microhttp')

class HTTPException(Exception):
    def __init__(self, status_code, msg):
        self.status_code = status_code
        self.message = msg

class Request(object):
    TOTAL_TIME = 0
    TOTAL_REQ = 0

    def __init__(self, reader, can_keep_alive=False):
        self.headers = {}
        self.reader = reader
        self.can_keep_alive = can_keep_alive
        self.keep_alive = False
        self.http_version = ''
        self.full_path = ''
        self.start_time = time.time()
        self.body_consumed = False

    @coroutine   
    def _nextline(self):
        return (yield from self.reader.readline()).decode('latin-1').rstrip()

    @coroutine
    def consume_headers(self):
        top_line = yield from self._nextline()
        self.method, self.full_path, self.http_version = top_line.split()
        self.method = self.method.upper()
        self.full_path = self.full_path.lower()
        self.http_version = self.http_version.lower()

        while True:
            line = yield from self._nextline()
            if not line:
                break
            key, value = line.split(':', 1)
            self.headers[key.lower()] = value.strip().lower()

        host = self.headers['host']
        self.url = 'http://' + host + self.full_path

        parts = urllib.parse.urlparse(self.url)
        self.path = parts.path or '/'

        log.debug('headers parsed %s', self.headers)

        if self.can_keep_alive:

            if self.headers.get('connection') == 'keep-alive':
                self.keep_alive = True
            elif self.http_version == 'http/1.1':
                self.keep_alive = True
                if self.headers.get('connection') == 'close':
                    self.keep_alive = False
            
            if self.keep_alive:
                log.debug('%s KEEP ALIVE REQUEST', self.http_version)



    @coroutine
    def body(self):
        if self.body_consumed:
            raise Exception('Body already consumed')

        l = 0
        if 'content-length' in self.headers:
            l = int(self.headers['content-length'])
        b = yield from self.reader.readexactly(l)
        self.body_consumed = True
        return b
    
    def end(self):
        dur = time.time() - self.start_time
        Request.TOTAL_REQ += 1
        Request.TOTAL_TIME += dur
        log.info('%s %s %s %sms', self.status_code, self.method, 
                self.full_path, round(dur*1000, 2))
        log.debug('avg req time %0.6f for %d requests',
            Request.TOTAL_TIME / Request.TOTAL_REQ, Request.TOTAL_REQ)


class Response(object):
    def __init__(self, writer, request, server):
        self.writer = writer
        self.request = request
        self.server = server
        self.length = 0
        self.is_sent = False
        self.is_head_request = False
        self.headers_sent = False
        self.status_code = -1
    
    @coroutine
    def send_headers(self, length=0, status=200, headers={}):
        log.debug('in send headers')
        self.write('{0} {1} {2}\r\n'.format('HTTP/1.0', status, 'OK'))
        self.write('Content-Length:{0}\r\n'.format(length))
        if self.request.keep_alive:
            self.write('Connection: Keep-Alive\r\n')
        self.write('\r\n')
        #yield from self.writer.drain()
        log.debug('headers written')
        self.headers_sent = True
        self.request.status_code = status
    
    @coroutine
    def send(self, d):
        self.write(d)
        yield from self.close()

    def write(self, d):
        if self.is_head_request and self.headers_sent:
            log.debug('head request')
            return
        if isinstance(d, str):
            d = d.encode('utf-8')
        self.writer.write(d)
       
    @coroutine
    def close(self):
        yield from self.writer.drain()
        log.debug('drained')
        self.request.end()
        
        # we've completed this request
        self.is_sent = True

        if not self.request.keep_alive:
            log.debug('closing connection')
            self.writer.close()
        else:
            log.debug('ready for next connection')
            yield from self.server.recycle(self.request, self)

class HTTPServer(object):
    def __init__(self, application):
        self.c = 0
        self.app = application
        self.can_keep_alive = True

    def serve(self, host='127.0.0.1', port=8888, keep_alive=False):
        # code from asyncio/streams.py start_server(), modified to use
        # _ReaderWrapper, otherwise we'd just use start_server() here
        self.can_keep_alive = keep_alive
        loop = asyncio.get_event_loop()

        serv = start_server(self.client_connected, host, port, loop=loop)
        try:
            loop.run_until_complete(serv)
            loop.run_forever()
        except KeyboardInterrupt as k:
            log.info('received keyboard interrupt, ending loop')
        finally:
            #serv.close()
            loop.close()

    def select_callback(self, path, meth):
        def cb_wrapper(cb, cb_kwargs, is_head=False):
            @coroutine
            def f(req, res):
                res.is_head_request = is_head
                kwargs = cb_kwargs
                
                # figure out whether cb wants request and response objects
                cb_args = inspect.getargspec(cb).args
                log.debug('cb args=%s', cb_args)
                if 'request' in cb_args:
                    kwargs['request'] = req
                if 'response' in cb_args:
                    kwargs['response'] = res
                log.debug('kwargs for %s are %s', cb.__name__, kwargs)
                # handle coroutines and vanilla
                try:
                    if inspect.isgeneratorfunction(cb):
                        log.debug('callback is generator')
                        r = yield from cb(**kwargs)
                    else:
                        log.debug('callback is not generator')
                        r = cb(**kwargs)

                    # see if callback already sent reply, and if not send its
                    # return value
                    if not res.is_sent:
                        if r is None: r = ''
                        log.debug('sending reply of length %d', len(r))
                        yield from res.send_headers(length=len(r))
                        yield from res.send(r)
                
                except HTTPException as e:
                    yield from self.send_error(res,e.status_code,msg=e.message)
                except ConnectionResetError as e:
                    log.debug('connection reset by peer')
                except Exception as e:
                    yield from self.send_error(res, 500, exception=e)

            # callback is all wrapped up and ready to be scheduled!    
            return f

        cb = None

        for pattern, method, callback, kwargs in self.app.routes:
            m = pattern.match(path)
            if not m: continue

            cb_kwargs = m.groupdict()
            cb_kwargs.update(kwargs)
            
            if meth.lower() == method.lower():
                cb = cb_wrapper(callback, cb_kwargs)
            elif meth.lower() == 'head':
                cb = cb_wrapper(callback, cb_kwargs, is_head=True)
            if cb:
                break # we matched one
        return cb

    @coroutine
    def handle(self, reader, writer, reused=False):
        request = Request(reader, self.can_keep_alive)
        try:
            yield from request.consume_headers()
        except Exception as e:
            log.debug('client disconnect')
            writer.close()
            return
        log.debug('NEW REQUEST %s', '(reused connection)' if reused else '')
        response = Response(writer, request, self)

        cb = self.select_callback(request.path, request.method)
        if not cb:
            yield from self.send_error(response, 404)
        else:
            yield from cb(request, response)

    @coroutine
    def recycle(self, request, response):
        # ok, recycle this reader and writer for a new connection
        # used for keep alive requests
        
        # first make sure we consumed the body
        log.debug('recycling')
        if not request.body_consumed:
            log.debug('consuming body')
            yield from request.body()
            log.debug('done')

        Task(self.handle(request.reader, response.writer, True))
    
    @coroutine
    def send_error(self, response, status=500, msg='', exception=None):
        log.debug('sending %s', status)
        yield from response.send_headers(status=status,length=len(msg))
        yield from response.send(msg)
        if exception: log.exception(exception)


    def client_connected(self, client_reader, client_writer):
        # a new client connected, our reader _ReaderWrapper, will let
        # us know when a subsequent http request arrives after this one
        # but we need to keep a handle on the writer
        client_reader._writer = client_writer

        Task(self.handle(client_reader, client_writer))

class Application(object):
    def __init__(self, routes):
        self.routes = []
        for route in routes:
            pattern, meth, cb  = route[:3]
            if len(route) == 4: kwargs = route[3]
            else: kwargs = {}

            self.routes.append((re.compile(pattern), meth, cb, kwargs))

    @classmethod
    @coroutine
    def static(cls, doc_root, response, path):
        log.debug('in static')
        chunk_size = 10*1024
        f = os.path.join(doc_root, path)
        if not os.path.exists(f):
            raise HTTPException(404, '{} does not exist'.format(f))
        L = os.stat(f).st_size
        yield from response.send_headers(length=L)
        log.debug('serving %s size %d', f, L)
        
        with open(f, 'rb') as the_file:
            while True:
                data = the_file.read(chunk_size)
                if not data:
                    break
                response.write(data)
        yield from response.close()

    def serve(self, host, port, **kwargs):
        HTTPServer(self).serve(host, port, **kwargs)


