---
title: Harbour Notes
tags: [demo, fixture]
---

# Harbour Notes

An original sample document used to test the Markdown processor. It has a short
introduction that wraps over two lines.

## Tides

The tide table is read twice a day. Low water comes first; high water follows about six hours later.

### Spring tides

Spring tides are the largest. See the [tide chart](https://example.invalid/chart) for details, and
the ![harbour map](https://example.invalid/map.png) for the layout. Raw <b>tags</b> are dropped.

- Check the gauge
- Log the reading
  - Note the time
- Report anomalies

1. First step
2. Second step

> The sea is calm today.
> Visibility is good.

Neap tides
----------

Neap tides are the smallest. They have a Setext heading above.

## Tools

```python
def depth(gauge):
    if gauge > 3:   # keep   spacing here
        return "deep"

    return "shallow"
```

| Tool | Use |
| --- | --- |
| Gauge | Reads depth |
| Log | Records data |

<!-- an editorial comment that must not appear -->

***

Final paragraph &amp; sign-off.
