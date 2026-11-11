# Procfile – required by Toolforge Build Service
# Each line becomes an executable command inside the container.
#
# "run-bot" is the main pipeline job command, scheduled hourly by
# toolforge/job.yaml. There is no web process: the bot is a cron job.
#
# Note: process type names must NOT collide with real binaries.
#
# Do NOT run `toolforge webservice buildservice start` on an image built from
# this branch. The webservice runs the FIRST entry here, which is run-bot, so
# Kubernetes keeps restarting the bot as a "website": on pid-bangladesh-
# uploadbot2 that meant an extra bot run every ~7 minutes alongside the hourly
# job, and the same photos recorded again and again in the PIDDateData ledger.

run-bot: python main.py
