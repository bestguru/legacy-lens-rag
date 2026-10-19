# ADR-002: Decoupling Terminal Protocol State into a Headless VT100/ANSI Engine

- **Status:** Accepted
- **Date:** 2026-06-29
- **Domain:** TACT Client Terminal Emulation & Device Automation
- **Related Rules:** `BR-TERM-201`, `BR-TERM-202`

## Context

In the legacy WinForms client, SSH/Telnet byte streams were fed directly into a commercial third-party UI terminal control. Automated batch scripts (such as 116-step router provisioning) relied on UI thread timers and synchronous `Control.Refresh()` calls inside sub-50ms polling loops (`BR-TERM-201`). This caused:
1. Full-screen GDI+ repaints on every poll iteration, pegging a CPU core at 100%.
2. Non-deterministic prompt detection failures whenever the Windows message pump was delayed by user window dragging or modal dialogs.
3. Zero ability to run automated regression tests in headless CI environments.

## Decision

We designed and implemented a **Headless VT100/ANSI State Machine Engine (`HeadlessVtEngine`)** that owns all protocol parsing, scrollback buffer state, and prompt-matching logic (`BR-TERM-202`):

- **Protocol Ownership:** The headless engine consumes raw SSH/Telnet byte streams on a background worker, processes ANSI escape sequences, and evaluates prompt-wait predicates deterministically without touching the WinForms UI thread.
- **UI Demotion:** The visible WinForms control is demoted to a passive render surface that invalidates at a capped frame rate (30 FPS coalesced render) rather than synchronous `Refresh()` per packet.
- **Benchmark Proof:** A 116-step equipment configuration script (58 command sends + 58 prompt waits) completed in **4,858 ms end-to-end** inside the `RACTTerminalScriptVerify` harness with zero UI thread dependency.
