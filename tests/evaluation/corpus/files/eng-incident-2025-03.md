# Postmortem: database disk exhaustion, March 2025

## Summary

On 12 March 2025 the primary database ran out of disk space at 03:10 UTC. The public API was unavailable for 47 minutes. No data was lost.

## Root cause

Archived write-ahead logs were not cleaned up after a failed backup job, and they slowly filled the data volume.

## Actions

Disk usage alerts now fire at 80 percent. The backup job reports its status to the on-call channel, and the log cleanup runs independently of the backup job.

## Lessons

Alerts that nobody receives are not alerts. The on-call rotation now checks the alert routing at every handover.
