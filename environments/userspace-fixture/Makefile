CC ?= cc
CFLAGS ?= -O2 -g -Wall -Wextra -Werror
PREFIX ?= /usr
DESTDIR ?=

.PHONY: all install clean
all: quirkbench-health

quirkbench-health: health.c
	$(CC) $(CFLAGS) -o $@ $<

install: all
	install -D -m 0755 quirkbench-health "$(DESTDIR)$(PREFIX)/bin/quirkbench-health"

clean:
	rm -f quirkbench-health
