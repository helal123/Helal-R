"""Copyright (c) 2018 Chaquo Ltd. All rights reserved."""

from __future__ import absolute_import, division, print_function

from android.util import Log
from io import StringIO


# LOGGER_ENTRY_MAX_PAYLOAD is defined as 4076 in
# android/platform/system/core/include/cutils/logger.h, but it looks like the level marker and
# tag are also included in this value.
MAX_LINE_LEN = 4060


class LogOutputStream(StringIO):
    def __init__(self, level, tag):
        StringIO.__init__(self)
        self.level = level
        self.tag = tag

    # print() calls write() separately to write the ending newline, which will unfortunately
    # produce multiple log messages. The only alternatives would be buffering, or ignoring
    # empty lines, both of which would be bad for debugging.
    def write(self, s):
        for line in s.splitlines():
            line = line or " "  # Empty log messages are ignored.
            while True:
                Log.println(self.level, self.tag, line[:MAX_LINE_LEN])
                line = line[MAX_LINE_LEN:]
                if not line:
                    break
