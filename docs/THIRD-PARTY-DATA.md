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

## Yale Bright Star Catalogue (automatic star matching)

`src/magi/data/star-catalog.json` (shipped in the repository and
copied to `<station root>/star-catalog.json` on first use) is a
filtered, reformatted subset of the Yale Bright Star Catalogue, 5th
Revised Ed. (Hoffleit & Warren 1991), Astronomical Data Center / CDS
catalog V/50. It includes Harvard Revised numbers, J2000
coordinates, and Johnson V magnitudes of stars at magnitude 6 or
brighter (5080 stars). Shipping it with Magi means a fresh station
solves with no catalog download over its limited link while deployed.

- Official source: CDS catalog V/50 (Astronomical Data Center).
- Retrieved snapshot: https://raw.githubusercontent.com/brettonw/YaleBrightStarCatalog/master/bsc5.json
- Retrieved file SHA-256: 08b019f560a20fdf67565604b2ad04c3e40938f71a4a7c187af2b88fad1a6e8b
- The catalog data is U.S. NSSDC/ADC public data. The mirror
  repository's MIT license covers the mirror author's conversion
  scripts, not the underlying catalog.
- Modifications: parsed sexagesimal positions to degrees, retained
  HR numbers as IDs, selected numeric fields, filtered by V magnitude,
  and converted them to JSON. The cache records the exact source
  SHA-256, provenance, and the approximation that proper motion is not
  applied. No endorsement by Yale, ADC, CDS, or the mirror author is
  implied.
