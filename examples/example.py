from microhttp import Application
import logging
import sys
from asyncio import coroutine, sleep
import argparse

# simplest handler taking no arguments
def hi():
    return 'hi'

@coroutine
def hi3(response):
    yield from response.send_headers(length=3)
    response.write('hi3')
    yield from response.close()

# coroutine that sleeps
@coroutine
def slow(response, t):
    body = 'IM SLOW'
    yield from response.send_headers(length=len(body))
    response.write(body[:2])
    yield from sleep(float(t))
    response.write(body[2:])
    yield from response.close()


# coroutine handler which receives the request and response object
@coroutine
def echo(request, response):
    body = yield from request.body()
    yield from response.send_headers(length=len(body))
    response.send(body)

# coroutine handler receiving arguments from re patterns in the route path
@coroutine
def groups(g1, g2):
    return 'groups! {0} {1}'.format(g1, g2)

if __name__ == '__main__':
    logging.basicConfig(stream=sys.stdout, level=logging.ERROR,
                format='%(asctime)s | %(levelname)s | %(message)s')

    p = argparse.ArgumentParser()
    p.add_argument('-l', '--log-level', default='ERROR')

    args = p.parse_args()

    logging.getLogger().setLevel(getattr(logging, args.log_level))

    a = Application([
        (r'^/echo$', 'POST', echo),
        (r'^/slow/(?P<t>.*)', 'GET', slow),
        (r'^/groups/(?P<g1>.*)/(?P<g2>.*)$', 'GET', groups),
        (r'^/site/(?P<path>.*)$', 'GET', Application.static,
           {'doc_root':'/tmp/site'}),
        (r'^/hi3', 'GET', hi3),
        (r'^/.*$', 'GET', hi),
        ])

    a.serve('0.0.0.0', 2020, keep_alive=True)

