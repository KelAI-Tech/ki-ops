# PR #37 canary test — forced resubmit against the live UAT Flex book (2026-09-14)

End-to-end test of the `--force` / unfinalized-order semantics on CANARY
(UAT Flex sim, db-canary ledger, `portfolio_canary` shares), run against the
real book left by this morning's submits: 1379 filled orders, ~390 risk-
"rejected" (booked UNFINALIZED) SELLs, 368 confirmed-cancelled orders and 22
cancels still in flight.

Everything ran at commit `3475087` via image `kelaidata/kotl-submit:pr37-test`
(digest `sha256:4ceae085…`) and Batch job definition `kotl-submit-pr37-test:1`
(copy of `kotl-submit:1`, image swapped; prod `:latest` and the `kotl-submit`
job definition untouched). A helper image `kotl-submit:pr37-tools` /
job definition `kotl-submit-pr37-tools:1` ran the read/cancel gRPC script.

## Phase A — protected no-op (forced re-run must not resend unfinalized)

Dry-run then live, replicating the morning DAG command plus `--force`
(Batch jobs `05b3a72f` dry, `5388c71c` live; log streams
`kotl-submit-pr37-test/default/d00bb519…` and `…/2ef49ca0…`). Both printed:

```
--force: already-sent source overridden to flex — forced re-runs compute the residual from live GetOrderInfo2 state: ...
flex order states (status / finalization / cancel):
  1379 × FILLED / FINALIZED / CANCEL_ORIGINAL — ALIVE — fully counted as sent
  392 × LOCATE_FAILED / UNFINALIZED / CANCEL_ORIGINAL — ALIVE — fully counted as sent
  368 × CANCELLED / UNFINALIZED / CANCELED — confirmed dead — remainder resendable under --force
  22 × CANCELLED / UNFINALIZED / CANCEL_REQUESTED — ALIVE — fully counted as sent
TARGET COVERED: every 2026-09-14 UAT delta was already sent — nothing to submit (clean no-op)
```

`order_count: 0`; ledger row counts unchanged (1379 done + 782 cancelled).
The 392 parked unfinalized orders and the 22 in-flight cancels were fully
counted as sent — nothing resent. 22 overshoot names were clipped to zero
with the loud no-corrective-order warning.

## Phase B — confirmed-dead resend (the operator cancel→confirm→force flow)

Sample: three zero-fill unfinalized SELLs from the morning forced run —
`74c02d50-…-73` CHA.US −309, `74c02d50-…-98` CXM.US −126, `74c02d50-…-185`
KHC.US −52. Cancelled via `CancelOrders` (job `493a07c4`), each polled to the
terminal ack: `CANCELLED / UNFINALIZED / CANCELED`, fills still 0.

Forced dry-run (job `5d7a0886`): the state summary moved exactly 3 orders
(389 alive-unfinalized / 371 confirmed-dead), and the residual audit CSV
freed exactly the dead remainder and nothing else:

```
CHA.US,-309.0,0,-309.0,fresh
CXM.US,-126.0,0,-126.0,fresh
KHC.US,-52.0,0,-52.0,fresh
```

Forced live run (job `0d73c20d`): printed
`--force: proceeding past the existing 2026-09-14 UAT claim (430cd439-…) — this send is capped to the residual`
and created exactly 3 replacement orders, batch
`0a249fca-5ec8-4659-92fc-0f9c65da50ad` (-1 CHA 309, -2 CXM 126, -3 KHC 52),
booked in the ledger with the same quantities. The UAT sim risk-rejected them
again (missing Avg Volume / Beta), i.e. they came back UNFINALIZED — and a
final forced dry-run (job `a793c342`) showed them ALIVE (392 alive-unfinalized
again) with `TARGET COVERED … clean no-op`: replacements are protected.

## Phase C — refresh, ledger migration, trade-file dispositions

`kotl refresh --trade-date 2026-09-14 --source live --trade-file-out …`
(jobs `4bcb0f8b`, `6581712e`): `updated_count: 2164`,
`trade_file_rows_updated: 392`.

- Auto-migration added `finalization_status`, `cancel_status`,
  `rejection_reason` to `kotl_working_orders` on db-canary and the refresh
  populated all 2164 rows: 1379 done/FINALIZED, 392 cancelled/UNFINALIZED/
  CANCEL_ORIGINAL (parked, incl. the replacements with their Flex rejection
  reasons), 371 …/CANCELED (confirmed dead, incl. the sample), 22
  …/CANCEL_REQUESTED.
- Back-filled `Trades_20260914.csv`: 389 rows `final_status=unfinalized`,
  3 rows `cancelled` (exactly the sample).
- A composite file also containing filled first-submit orders and the Phase B
  replacements back-filled to `filled` (filled_qty = qty, FINALIZED) /
  `unfinalized` / `cancelled` respectively —
  `s3://kelaitrading/trades/canary/pr37_test/Trades_20260914_pr37_composite.csv`.

## Verdict

PR #37's contract held on every phase: unfinalized "rejected" orders were
never resent, only the confirmed-CANCELED remainder was freed (exactly, to
the share), the claim/force and recon guards behaved as documented, and the
new workflow columns + trade-file dispositions round-trip correctly. One
note, not a defect: the legacy ledger `status` column still shows
`cancelled` for parked unfinalized orders — the truth lives in the new
workflow columns, which is what both the residual logic (live Flex) and the
trade-file `final_status` use.
