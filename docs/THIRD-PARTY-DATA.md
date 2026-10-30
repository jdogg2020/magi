# Third-party catalogue data

`src/magi/data/bright-stars.json` is a filtered, reformatted subset of the
International Astronomical Union (IAU), Division C Working Group on Star Names,
**IAU Catalog of Star Names (WGSN)**. It includes names, J2000 coordinates and
catalogue magnitudes of named stars brighter than magnitude 4.

- Official source: https://www.iau.org/public/themes/naming_stars/
- Retrieved text snapshot: https://raw.githubusercontent.com/mirandadam/iau-starnames/master/catalog_data/IAU-CSN.txt
- Source text identifies its update date as 2022-04-04. It is a frozen subset,
  not a claim to include the latest approved names.
- The source header states that IAU-produced products are released under
  **Creative Commons Attribution**, with attribution to the source required.
- Modifications: filtered by magnitude, retained ASCII catalogue names as IDs,
  selected numeric fields and converted them to JSON. The JSON records the
  exact source SHA-256, provenance and approximation that proper motion is not applied.

This attribution concerns the catalogue data. Magi's own software licence remains
the owner's separate, pending decision. No IAU endorsement is implied.
