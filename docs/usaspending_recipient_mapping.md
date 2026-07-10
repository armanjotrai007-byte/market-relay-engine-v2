# USAspending Recipient Mapping Review

This note documents the reviewed recipient mappings in `config/usaspending_recipient_ticker_map.yaml`.
The YAML file intentionally contains only fields accepted by the strict loader; source URLs live here.

## Verification Method

Mappings were verified from official USAspending award-detail API responses. Each selected response returned the configured `recipient.recipient_uei`, `recipient.recipient_name`, and, where available, a parent recipient tying the recipient to the public issuer.

USAspending recipient search is not complete parent-rollup coverage. These mappings are active confirmed prime-recipient identities for smoke and paper validation, not a claim that every issuer subsidiary UEI has been mapped.

## Active Mappings

| Ticker | Recipient legal name | Recipient UEI | Verification source | Ticker/issuer tie | Caveat |
| --- | --- | --- | --- | --- | --- |
| LMT | LOCKHEED MARTIN CORPORATION | G4KDGE4JFFK7 | USAspending award detail API: https://api.usaspending.gov/api/v2/awards/CONT_AWD_N0001920C0009_9700_-NONE-_-NONE-/ | USAspending parent recipient is LOCKHEED MARTIN CORP, UEI ZFN2JJXBLZT3. | One Lockheed Martin operating recipient; not exhaustive. |
| LMT | LOCKHEED MARTIN CORPORATION | XFJMYSYFJEK4 | USAspending award detail API: https://api.usaspending.gov/api/v2/awards/CONT_AWD_W31P4Q20C0023_9700_-NONE-_-NONE-/ | USAspending parent recipient is LOCKHEED MARTIN CORP, UEI ZFN2JJXBLZT3. | Separate Lockheed Martin recipient location/UEI. |
| RTX | RAYTHEON COMPANY | MZK8TCNF24G2 | USAspending award detail API: https://api.usaspending.gov/api/v2/awards/CONT_AWD_FA873017C0010_9700_-NONE-_-NONE-/ | USAspending parent recipient is RTX CORP, UEI PPLZG8J3N9D4. | Raytheon recipient only; other RTX businesses may have separate UEIs. |
| RTX | ROCKWELL COLLINS, INC. | J4Q3HP6NHK47 | USAspending award detail API: https://api.usaspending.gov/api/v2/awards/CONT_IDV_N0001924G0017_9700/ | USAspending parent recipient is RTX CORP, UEI PPLZG8J3N9D4. | Collins Aerospace/Rockwell Collins recipient; not a Pratt & Whitney mapping. |
| GD | GENERAL DYNAMICS INFORMATION TECHNOLOGY, INC. | SMNWM6HN79X5 | USAspending award detail API: https://api.usaspending.gov/api/v2/awards/CONT_AWD_693KA726F00001_6920_693KA718D00001_6920/ | USAspending parent recipient is GENERAL DYNAMICS CORP, UEI VF58HFRNGEL8. | GDIT recipient only; not all GD defense manufacturing entities. |
| GD | ELECTRIC BOAT CORPORATION | E7BEKJ4V9528 | USAspending award detail API: https://api.usaspending.gov/api/v2/awards/CONT_AWD_N0002417C2100_9700_-NONE-_-NONE-/ | USAspending parent recipient is GENERAL DYNAMICS CORP, UEI VF58HFRNGEL8. | Electric Boat recipient only. |
| GD | GENERAL DYNAMICS LAND SYSTEMS INC. | HAWKSQF848W7 | USAspending award detail API: https://api.usaspending.gov/api/v2/awards/CONT_IDV_W56HZV21D0001_9700/ | Legal recipient name is General Dynamics Land Systems Inc. | USAspending detail self-parents this UEI rather than showing the GD Corp parent on that record. |
| AVAV | AEROVIRONMENT, INC | MWKWXVSSC518 | USAspending award detail API: https://api.usaspending.gov/api/v2/awards/CONT_AWD_693JJ626C000014_6930_-NONE-_-NONE-/ | USAspending parent recipient is AEROVIRONMENT, INC, same UEI. | AeroVironment has multiple active UEIs; this is one confirmed prime recipient. |
| AVAV | AEROVIRONMENT, INC. | YJG1MDHLBC88 | USAspending award detail API: https://api.usaspending.gov/api/v2/awards/CONT_AWD_W31P4Q21C0029_9700_-NONE-_-NONE-/ | USAspending parent recipient is AEROVIRONMENT, INC., same UEI. | Separate AeroVironment recipient UEI at the same Simi Valley address. |
| PLTR | PALANTIR USG INC | HNN4F9JZWDY8 | USAspending award detail API: https://api.usaspending.gov/api/v2/awards/CONT_AWD_FA880623C0002_9700_-NONE-_-NONE-/ | USAspending parent recipient is PALANTIR TECHNOLOGIES INC., UEI FSY4LVSBGWB7. | Defense/government subsidiary recipient. |
| PLTR | PALANTIR TECHNOLOGIES INC. | FSY4LVSBGWB7 | USAspending award detail API: https://api.usaspending.gov/api/v2/awards/CONT_AWD_75N95025F00001_7529_75N95022D00025_7529/ | USAspending parent recipient is PALANTIR TECHNOLOGIES INC., same UEI. | Parent public issuer recipient; civilian and federal work may be separate from Palantir USG. |

## Intentionally Not Added

Oil-company UEIs were not added. The validation scope uses USAspending for defense contract context; oil context remains EIA/FRED/macro/proxy based.

No Northrop Grumman, Chevron, or other prior example tickers were kept in the active tradable universe because the selected universe is limited to PLTR, LMT, GD, RTX, AVAV, XOM, OXY, SLB, COP, and VLO.

No Pratt & Whitney mapping was added in this pass. A broad official award search timed out during review, and no specific Pratt & Whitney award-detail response was committed as confirmed. Do not add a Pratt UEI until an official USAspending/SAM record cleanly verifies the recipient UEI and RTX relationship.

General Dynamics Mission Systems and General Dynamics-OTS identities were investigated but not added in this pass. The official award details reviewed showed WICO LIMITED parent recipients for the sampled UEIs, so they need a separate parent/issuer review before being marked confirmed for ticker GD.
