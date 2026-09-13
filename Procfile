# Procfile – required by Toolforge Build Service
# Each line becomes an executable command inside the container.
#
# ORDER MATTERS. `toolforge webservice buildservice start` runs the FIRST entry
# in this file regardless of what it is named. `web` must therefore stay at the
# top: if `run-bot` were first, the webservice would launch the bot, which never
# listens on port 8000, and the webservice would fail to come up.
#
#   web      – the control panel, served at <tool>.toolforge.org
#   run-bot  – the hourly pipeline job, scheduled by toolforge/job.yaml
#
# Note: process type names must NOT collide with real binaries.

web: gunicorn --bind=0.0.0.0:8000 --workers=2 --timeout=60 --forwarded-allow-ips='*' panel.app:app
run-bot: python main.py
