#!/usr/bin/env python
"""
Container entrypoint: check the configuration, then run the command
(web server, task worker, migrate, ...).

Loading settings.py applies its checks (secrets, mail TLS options, ...). A
configuration error is reported as one line instead of a stack trace, and
the command is not started. Other errors keep their traceback: they are
bugs, not configuration problems.
"""
import importlib
import os
import sys


def main(argv):
  if not argv:
    sys.exit("usage: docker-entrypoint.py COMMAND [ARGS...]")
  os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'settings')
  from django.core.exceptions import ImproperlyConfigured
  try:
    importlib.import_module(os.environ['DJANGO_SETTINGS_MODULE'])
  except ImproperlyConfigured as e:
    print("Helios configuration error: %s" % e, file=sys.stderr)
    sys.exit(1)
  os.execvp(argv[0], argv)


if __name__ == '__main__':
  main(sys.argv[1:])
