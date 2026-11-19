# Deployment runbook

Deployments go through the pipeline in three stages: build, canary and full rollout.

## Canary

A canary release serves 10 percent of traffic for 20 minutes before the full rollout.

## Freeze window

There is a deploy freeze every Friday after 14:00 UTC. Emergency fixes need approval from the release manager.

## Rollback

Rollback is a one-click action in the pipeline and must complete within 10 minutes.
