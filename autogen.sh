#!/bin/sh
ACLOCAL="aclocal -I m4"
export ACLOCAL

autoreconf -i

test "z$*" = "z--no-configure" || ./configure "$@"
