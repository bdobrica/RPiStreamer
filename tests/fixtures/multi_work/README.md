# Multi-work contract fixtures

These fixtures freeze the feature contract before implementation:

- `mf_ghost_continuous` expands to 37 synthetic files split into work-local
  episode counts of 12, 12, and 13.
- `tsukimichi_reset` expands to 12 first-season and 25 second-season files,
  both starting at local episode 1.
- `tie_ins` contains a movie, OVA, summary, ambiguous bonus, and misleading
  resolution/year-numbered basename.

All MAL IDs in these fixtures are deliberately synthetic values in the
`990001`–`992004` range. Filenames are patterned after the reported layouts
but are not copied media names. The JSON files are compact contracts, not
provider response recordings. The INI files demonstrate that ordinary
episodes can be mapped with a few work rules rather than one section per file.

The current production sidecar parser does not accept these new sections yet;
Step 2 implements that behavior. `tests/test_multi_work_contract.py` validates
the frozen fixture shape and expected grouping independently in the meantime.
