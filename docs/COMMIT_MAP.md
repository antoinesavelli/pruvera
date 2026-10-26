# Commit map

On 2026-10-26 this repo's history was rewritten before its first public push (private paths, fixtures, results and
the old author address removed). Every commit hash changed. `COMMIT_MAP.tsv` maps the old full hash of each
surviving commit to its new one; a hash in an older plan, `docs/STATUS.md` or a registered study that is not in
the map belongs to a commit that touched only paths that are not published, and has no counterpart here.

```
awk -F'\t' -v h=<old-hash-prefix> 'index($1,h)==1 {print $2}' docs/COMMIT_MAP.tsv
```
