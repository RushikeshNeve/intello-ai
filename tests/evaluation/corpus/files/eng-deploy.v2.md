# Deployment runbook

Deployments go through the pipeline in three stages: build, canary and full rollout.

## Canary

A canary release serves 5 percent of traffic for 30 minutes before the full rollout.

## Freeze window

There is a deploy freeze every Thursday after 16:00 UTC. Emergency fixes need approval from the release manager.

## Rollback

Rollback is a one-click action in the pipeline and must complete within 10 minutes.
