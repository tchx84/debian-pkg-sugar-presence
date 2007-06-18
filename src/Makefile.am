servicedir = $(datadir)/dbus-1/services
service_DATA = org.laptop.Sugar.Presence.service

$(service_DATA): Makefile
	( \
	echo '[D-BUS Service]'; \
	echo 'Name = org.laptop.Sugar.Presence'; \
	echo 'Exec = $(bindir)/sugar-presence-service'; \
	) > $@

psdatadir = $(datadir)/sugar-presence-service
psdata_PYTHON = \
	__init__.py \
	activity.py \
	buddy.py \
	buddyiconcache.py \
	linklocal_plugin.py \
	presenceservice.py \
	pstest.py \
	psutils.py \
	server_plugin.py \
	telepathy_plugin.py

dist_bin_SCRIPTS = sugar-presence-service

DISTCLEANFILES = $(service_DATA)

dist_check_SCRIPTS = test_psutils.py

TESTS_ENVIRONMENT = \
		    PYTHONPATH=$(top_srcdir) \
		    $(PYTHON)
TESTS = $(dist_check_SCRIPTS)
