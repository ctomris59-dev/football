# Weekly reliability output

Primary weekly product is a ranked Friday-Monday prediction list from the frozen V1 model over markets actually executable in the official Turkish bulletin.

- Value is optional and remains a separate stricter filter.
- The primary list chooses the stronger V1 side of each binary target market when that side has a Turkish price.
- At most one primary pick is kept per fixture.
- Existing V1 data-quality/player-context/rest/injury gates remain active.
- When a fresh international reference exists, a strong contradiction beyond the existing divergence limit rejects the pick. Missing international reference does not erase the primary V1 forecast.
- `Yüksek Güven` is reserved for selected-side V1 probability >= 0.70. Lower-ranked items remain `Haftanın En Güvenilirleri`; probabilities are never inflated to satisfy a weekly quota.
- A weekly ranked list is allowed to finalize once official Turkish fixture coverage is >= 90% and at least the configured minimum number of executable ranked picks exists.
- Strict Value still requires the existing model + international no-vig + Turkish-price gates.
