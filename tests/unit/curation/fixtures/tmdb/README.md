# TMDB fixtures

Hand-authored in the shape of TMDB v3 responses (`/search/movie`, `/movie/{id}` with
`append_to_response=external_ids,keywords,credits`). Ids, IMDb ids and dates are the real
TMDB/IMDb values for the real films; vote counts, averages and other numbers are
approximate, and "Modern Life Is Rubbish (2014)" and "Modern Girls" are invented entries
that only exist to put superstring titles next to the query "Modern". They were not
recorded from the live API (no key was available when they were written); replacing them
with recorded responses changes no test logic.

- `searches.json`: list of `{query, primary_release_year (null = no year filter), results}`.
  A search with no entry returns empty results.
- `movies.json`: details keyed by TMDB id.
