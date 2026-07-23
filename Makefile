PREFIX ?= /usr/local
BINDIR ?= $(PREFIX)/bin
PYTHON ?= python3

.PHONY: install test

install:
	install -d "$(DESTDIR)$(BINDIR)"
	install -m 755 gitdag.py "$(DESTDIR)$(BINDIR)/gitdag"

test:
	$(PYTHON) -m unittest -v
