# Procfile – required by Toolforge Build Service
# Each line becomes an executable command inside the container.
#
# "run-bot" is the main pipeline job command, scheduled hourly by
# toolforge/job.yaml. There is no web process: the bot is a cron job.
#
# Note: process type names must NOT collide with real binaries.

run-bot: python main.py
