# Catalog definitions

One YAML file per curated catalog goes here; the file name is the catalog id
(`my_list.yaml` -> `my_list`). [docs/catalog-definition.example.yaml](../docs/catalog-definition.example.yaml)
shows every field. These files are your own configuration: keep them out of this repository.

The directory is mounted read-only into both services. A definition takes effect when it is
applied (`python -m app.curation apply`, and automatically before `python -m app.curation run`;
the worker will also apply on start from milestone 6). Applying validates every file, resolves
exclusion names to TMDB ids, and stores the accepted definitions in SQLite. An invalid file is
reported and the previously accepted version stays in force; a deleted file marks its catalog
removed.
