#!/usr/bin/env python
# vi: ts=4 ai noet 
#
# Copyright (C) 2006, Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

import logging
import sys
import os

from sugar import logger
from sugar import env


_logger = logging.getLogger('s-p-s')


def usage():
    _logger.debug("Usage: sugar-presence-service [<test buddy number (1 - 10)>] [randomize]")


def main():
	test_num = 0
	randomize = False
	if len(sys.argv) in [2, 3]:
	    try:
	        test_num = int(sys.argv[1])
	    except ValueError:
	        _logger.debug("Bad test user number.")
	    if test_num < 1 or test_num > 10:
	        _logger.debug("Bad test user number.")

	    if len(sys.argv) == 3 and sys.argv[2] == "randomize":
	        randomize = True
	elif len(sys.argv) == 1:
	    pass
	else:
	    usage()
	    os._exit(1)

	if test_num > 0:
	    logger.start('test-%d-presenceservice' % test_num)
	else:
	    logger.start('presenceservice')

	import presenceservice

	_logger.info('Starting presence service...')

	presenceservice.main(test_num, randomize)
