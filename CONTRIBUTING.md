# Contributing

Thanks for your interest! Ground rules:

- **Read-only is non-negotiable.** PRs adding write operations (create,
  update, delete, pause, budget, bid, keyword changes, bulk operations,
  applying or dismissing recommendations) will be declined regardless of
  quality; see PLAN.md §2. A management server belongs in a separate project.
- **Never include real advertiser data** in issues, PRs, fixtures, or test
  cases — synthetic data only. Never commit private keys.
- Run `python -m unittest discover tests` and
  `python scripts/check_api_drift.py` before submitting.
- API inventory updates: check out the newer commit of
  `apple/apple-ads-platform-api-python`, run
  `python scripts/extract_operations.py /path/to/checkout`, classify any
  new/changed operations in `scripts/generate_registry.py`, run
  `python scripts/generate_registry.py`, and update `docs/API_NOTES.md` if
  live behavior differs — all in one PR.
- New live-API discoveries (field names, limits, response quirks) belong in
  `docs/API_NOTES.md` with how you verified them.
