# Safety eval

| Metric | Result |
| --- | --- |
| Attacks refused | **102/102** |
| Exfiltration queries run (allowed SQL trying to pull out-of-scope data) | 56 |
| Result cells scanned for forbidden values | 43,144 |
| **Leaked values** | **0** |
| Scanner self-check: same queries with the permission layer switched off (maria) | 3,206 leaks detected |

## By user

| User | Scope | Attacks refused | Cells scanned | Forbidden values checked | Leaks |
| --- | --- | --- | --- | --- | --- |
| maria | region West only | 25/25 | 8,055 | 3,159 | 0 |
| dev | region East only | 25/25 | 9,483 | 2,690 | 0 |
| priya | all regions | 23/23 | 13,526 | 0 | 0 |
| sam | all regions | 29/29 | 12,080 | 110 | 0 |

## How refusals were reported

| Error code | Count |
| --- | --- |
| INVALID_SQL | 4 |
| PERMISSION_DENIED | 47 |
| TIMEOUT | 4 |
| WRITE_BLOCKED | 47 |
