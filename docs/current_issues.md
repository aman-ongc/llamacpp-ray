# Current Issues & Improvement Backlog

> Updated: 2026-06-01
> Status key: 🔴 Blocking · 🟡 Important · 🟢 Enhancement · ✅ Resolved

---

## Issue 1 - WS-11 browser access to dashboards

**Severity:** 🔴 Blocking
**Status:** ✅ Resolved

WS-11 browser access is working via the created `netsh interface portproxy` rules.

---

## Issue 2 - Grafana dashboard data visibility

**Severity:** 🔴 Blocking
**Status:** ✅ Resolved

Dashboard JSON was fixed and Grafana now loads the provisioned dashboard and datasource cleanly.

---

## Issue 3 - Ray multi-node cluster

**Severity:** 🔴 Blocking
**Status:** ✅ Resolved

Ray is now running across four GPU nodes:

- WS-11 `10.208.211.62`
- WS-03 `10.208.211.54`
- WS-08 `10.208.211.59`
- WS-13 `10.208.211.64`

`ray status` shows 4 active nodes and 4/4 GPUs in use.

---

## Issue 4 - Shutdown scripts

**Severity:** 🟡 Important
**Status:** ✅ Resolved

Linux, macOS, and Windows shutdown scripts were added under `startup_scripts/`.

---

## Issue 5 - API key metadata + per-key Grafana monitoring

**Severity:** 🟡 Important
**Status:** ✅ Resolved

Implemented:

- `api_keys.metadata`
- request log API-key prefix capture
- Grafana Postgres datasource
- per-user / per-key usage panel

---

## Issue 6 - Ray / Serve autostart on reboot

**Severity:** 🟡 Important
**Status:** ✅ Resolved

An `@reboot` cron entry was installed to start the Linux stack automatically after reboot.

---

## Issue 7 - Node exporter rollout

**Severity:** 🟡 Important
**Status:** 🟡 Partial

Node-exporter installation scripts are in place and have been exercised on the cluster nodes, but the final scrape verification pass should still be repeated after any network changes.

---

## Issue 8 - Default admin secret

**Severity:** 🔴 Blocking
**Status:** ✅ Resolved

`ADMIN_SECRET` is now set in `.env` to a non-default secret and the gateway was rebuilt/restarted with it.

---

## Issue 9 - Qwen thinking mode empty `content`

**Severity:** 🟢 Enhancement
**Status:** ✅ Resolved

Gateway request/response normalization was added so empty `content` falls back more gracefully and `/no_think` is injected when needed.

---

## Issue 10 - Worker health dashboard

**Severity:** 🟢 Enhancement
**Status:** 🟢 Open

Now that the cluster and monitoring path are working, a dedicated worker health dashboard is the next useful improvement.

---

## Summary

The distributed Ray inference path is up on all four workstations. The only remaining open improvement is the worker health dashboard; node-exporter verification can be tightened if the network layout changes again.
