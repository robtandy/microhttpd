#!/usr/bin/env python

from distutils.core import setup
import microhttpd

V = microhttpd.__version__

setup(name='microhttpd',
      version=V,
      author='Rob Tandy',
      author_email='rob.tandy@gmail.com',
      url='https://github.com/robtandy/microhttpd',
      long_description="""
      A simple, small and fast web server based around asyncio.
      """,
      py_modules=['microhttpd'],
)
