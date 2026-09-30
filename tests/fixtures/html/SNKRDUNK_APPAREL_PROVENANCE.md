These two small fixtures preserve the relevant structure of browser DOM captures
taken on 2026-09-30, including the inline apparel data in record `50` and the
`$4e:props:apparelData` reference from record `51`. JSON-LD contains no Product.

| Fixture | Observed parent | Original capture SHA-256 |
| --- | --- | --- |
| `snkrdunk_apparel_inline_flight.html` | `/apparels/300058` | `e80de1bd699a3af6b3e025604097817613ff524e292bb11861973a8cb3baeee0` |
| `snkrdunk_apparel_referenced_flight.html` | `/apparels/721913` | `d2b9b5b0cd3b973a931881e9af62248e2f65701a95860d2711afc794d2d78295` |

Names, images, component names and variant/size IDs are anonymized. Only a few
observed quantity variants and relevant fields are retained. Prices and numeric
listing counts preserve the observed sell/bid distinction. Original full HTML,
advertising, tracking and unrelated content are not stored in this repository.

Both captures show a parent product with new single-item listings. They do not
prove a sold-parent or used-listing Flight contract. The new parser therefore
leaves missing/zero/ambiguous single-item availability unknown and preserves
the existing verified NextData/JSON-LD routes for sold and used products.

The `self.__next_f.push` scripts are serialized data; parsing them requires no
JavaScript execution. Static HTTP can use this parser if its successful response
contains the same scripts. Browser DOM captures alone do not prove the current
production HTTP response, access status or fetch behavior.
