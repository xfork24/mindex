# 人物库出镜审核读取耗时基线

> 历史记录（2026-10-23 由 `scripts/profile_person_review.py` 生成的本机基线），内容可能与当前代码不符，重新测量以该脚本输出为准。

- 数据目录：`/Users/hengliu/workspace/mindex/data`；每组 1 次预热 + 3 次计时；单位毫秒；进程内 TestClient，不含网络与浏览器。
- 单元格为 p50 / p95。`assemble` 为 Python 端组装（总耗时减去各阶段与 middleware）。

## 数据规模

| 项目 | 数值 |
| --- | --- |
| person_evidence 行数 | 54,510 |
| segments 行数 | 5,359 |
| person_coverage 行数 | 3,908 |
| person_entities 行数 | 59 |
| person_versions 行数 | 1 |
| person_evidence.data 平均 / 最大字节 | 6820 / 7978 |
| person_versions 最大体积（字节） | 85,817,921 |
| 证据状态 accepted | 11,088 |
| 证据状态 candidate | 18,513 |
| 证据状态 rejected | 24,909 |

## 审核列表 `GET /api/persons/review/shots`（limit=50）

| 素材(证据行/分镜) | 状态 | 筛选 | p50 / p95 total ms | filters | scope_total | page | evidence | json_decode | assemble | middleware | 响应 KB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 074e80(35955/1126) | candidate | 无 | 1701 / 1706 | 1073 / 1086 | 309 / 314 | 309 / 311 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 |
| 074e80(35955/1126) | accepted | 无 | 4295 / 4808 | 1499 / 1642 | 1118 / 1486 | 1486 / 1488 | 36 / 37 | 67 / 67 | 0 / 0 | 30 / 31 | 1718 |
| 074e80(35955/1126) | accepted | 人物 | 4218 / 4234 | 1481 / 1508 | 1115 / 1119 | 1489 / 1490 | 30 / 30 | 36 / 37 | 0 / 0 | 19 / 19 | 1063 |
| 074e80(35955/1126) | rejected | 无 | 756 / 770 | 0 / 0 | 321 / 333 | 332 / 334 | 31 / 33 | 20 / 21 | 0 / 15 | 12 / 12 | 614 |
| 3a3cad(18555/333) | candidate | 无 | 1937 / 2301 | 1181 / 1558 | 377 / 378 | 377 / 378 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 |
| 3a3cad(18555/333) | accepted | 无 | 3393 / 4045 | 1210 / 1891 | 799 / 809 | 1195 / 1218 | 36 / 36 | 50 / 50 | 0 / 0 | 23 / 23 | 1257 |
| 3a3cad(18555/333) | accepted | 人物 | 3528 / 3585 | 1206 / 1209 | 805 / 805 | 1383 / 1414 | 27 / 28 | 43 / 43 | 0 / 0 | 20 / 21 | 1103 |
| 3a3cad(18555/333) | rejected | 无 | 787 / 894 | 0 / 0 | 374 / 422 | 374 / 432 | 27 / 27 | 4 / 4 | 0 / 0 | 2 / 2 | 110 |

## 抽样 SQL 的 EXPLAIN QUERY PLAN（accepted、最大素材）

**查询 1**（4,183 字符）
```
   2    0  CO-ROUTINE memberships
   3    2  COMPOUND QUERY
   4    3  LEFT-MOST SUBQUERY
   7    4  MATERIALIZE confirmed
   9    7  CO-ROUTINE k
  10    9  COMPOUND QUERY
  11   10  LEFT-MOST SUBQUERY
  14   11  MATERIALIZE valid_pairs
  24   14  SCAN ps USING INDEX sqlite_autoindex_person_sources_1
  39   14  SEARCH role USING AUTOMATIC PARTIAL COVERING INDEX (kind=?)
  44   14  CORRELATED SCALAR SUBQUERY 1
  48   44  SCAN json_each VIRTUAL TABLE INDEX 3:
  62   14  SCAN performer VIRTUAL TABLE INDEX 3:
  68   14  SEARCH actor USING INDEX sqlite_autoindex_person_entities_1 (id=?)
  88   14  USE TEMP B-TREE FOR DISTINCT
  92   11  MATERIALIZE marks
 100   92  SCAN person_evidence USING INDEX person_evidence_shot
 139   11  MATERIALIZE removed
 147  139  SCAN person_coverage USING INDEX person_coverage_shot
 190   11  SEARCH e USING INDEX person_evidence_shot (source_id=?)
 195   11  SEARCH s USING INDEX sqlite_autoindex_segments_1 (id=?)
 209   11  BLOOM FILTER ON pair (source_id=? AND role_id=? AND actor_id=?)
 220   11  SEARCH pair USING AUTOMATIC COVERING INDEX (source_id=? AND role_id=? AND actor_id=?) LEFT-JOIN
 242   11  SEARCH marks USING AUTOMATIC COVERING INDEX (source_id=? AND shot_id=?) LEFT-JOIN
 260   11  SEARCH removed USING AUTOMATIC COVERING INDEX (source_id=?) LEFT-JOIN
 347   10  UNION ALL
 354  347  SEARCH c USING COVERING INDEX person_coverage_shot (source_id=?)
 360  347  SEARCH s USING INDEX sqlite_autoindex_segments_1 (id=?)
 372  347  CORRELATED SCALAR SUBQUERY 6
 376  372  SEARCH e USING COVERING INDEX person_evidence_shot (source_id=? AND shot_id=?)
 398  347  USE TEMP B-TREE FOR DISTINCT
 406    7  SEARCH e USING INDEX person_evidence_shot (source_id=?)
 416    7  CORRELATED SCALAR SUBQUERY 16
 430  416  SEARCH role USING INDEX sqlite_autoindex_person_entities_1 (id=?)
 438  416  SEARCH actor USING INDEX sqlite_autoindex_person_entities_1 (id=?)
 446  416  SEARCH ps USING INDEX sqlite_autoindex_person_sources_1 (source_id=?)
 451  416  SCAN json_each EXISTS VIRTUAL TABLE INDEX 3:
 460  416  SCAN json_each EXISTS VIRTUAL TABLE INDEX 3:
 477    7  SEARCH s USING INDEX sqlite_autoindex_segments_1 (id=?)
 507    7  SEARCH k USING AUTOMATIC PARTIAL COVERING INDEX (source_id=? AND shot_id=?)
 527    7  USE TEMP B-TREE FOR DISTINCT
 531    4  SCAN confirmed
 539    3  UNION ALL
 541  539  CO-ROUTINE k
 542  541  COMPOUND QUERY
 543  542  LEFT-MOST SUBQUERY
 546  543  MATERIALIZE valid_pairs
 556  546  SCAN ps USING INDEX sqlite_autoindex_person_sources_1
 571  546  SEARCH role USING AUTOMATIC PARTIAL COVERING INDEX (kind=?)
 576  546  CORRELATED SCALAR SUBQUERY 1
 580  576  SCAN json_each VIRTUAL TABLE INDEX 3:
 594  546  SCAN performer VIRTUAL TABLE INDEX 3:
 600  546  SEARCH actor USING INDEX sqlite_autoindex_person_entities_1 (id=?)
 620  546  USE TEMP B-TREE FOR DISTINCT
 624  543  MATERIALIZE marks
 632  624  SCAN person_evidence USING INDEX person_evidence_shot
 671  543  MATERIALIZE removed
 679  671  SCAN person_coverage USING INDEX person_coverage_shot
 724  543  SEARCH e USING INDEX person_evidence_shot (source_id=?)
 729  543  SEARCH s USING INDEX sqlite_autoindex_segments_1 (id=?)
 743  543  BLOOM FILTER ON pair (source_id=? AND role_id=? AND actor_id=?)
 754  543  SEARCH pair USING AUTOMATIC COVERING INDEX (source_id=? AND role_id=? AND actor_id=?) LEFT-JOIN
 776  543  SEARCH marks USING AUTOMATIC COVERING INDEX (source_id=? AND shot_id=?) LEFT-JOIN
 794  543  SEARCH removed USING AUTOMATIC COVERING INDEX (source_id=?) LEFT-JOIN
 883  542  UNION ALL
 894  883  SEARCH c USING COVERING INDEX person_coverage_shot (source_id=?)
 900  883  SEARCH s USING INDEX sqlite_autoindex_segments_1 (id=?)
 912  883  CORRELATED SCALAR SUBQUERY 6
 916  912  SEARCH e USING COVERING INDEX person_evidence_shot (source_id=? AND shot_id=?)
 938  883  USE TEMP B-TREE FOR DISTINCT
 942  539  MATERIALIZE ranked
 945  942  CO-ROUTINE (subquery-23)
 947  945  CO-ROUTINE k
 948  947  COMPOUND QUERY
 949  948  LEFT-MOST SUBQUERY
 952  949  MATERIALIZE valid_pairs
 962  952  SCAN ps USING INDEX sqlite_autoindex_person_sources_1
 977  952  SEARCH role USING AUTOMATIC PARTIAL COVERING INDEX (kind=?)
 982  952  CORRELATED SCALAR SUBQUERY 1
 986  982  SCAN json_each VIRTUAL TABLE INDEX 3:
1000  952  SCAN performer VIRTUAL TABLE INDEX 3:
1006  952  SEARCH actor USING INDEX sqlite_autoindex_person_entities_1 (id=?)
1026  952  USE TEMP B-TREE FOR DISTINCT
1030  949  MATERIALIZE marks
1038 1030  SCAN person_evidence USING INDEX person_evidence_shot
1077  949  MATERIALIZE removed
1085 1077  SCAN person_coverage USING INDEX person_coverage_shot
1128  949  SEARCH e USING INDEX person_evidence_shot (source_id=?)
1133  949  SEARCH s USING INDEX sqlite_autoindex_segments_1 (id=?)
1147  949  BLOOM FILTER ON pair (source_id=? AND role_id=? AND actor_id=?)
1158  949  SEARCH pair USING AUTOMATIC COVERING INDEX (source_id=? AND role_id=? AND actor_id=?) LEFT-JOIN
1180  949  SEARCH marks USING AUTOMATIC COVERING INDEX (source_id=? AND shot_id=?) LEFT-JOIN
1198  949  SEARCH removed USING AUTOMATIC COVERING INDEX (source_id=?) LEFT-JOIN
1285  948  UNION ALL
1292 1285  SEARCH c USING COVERING INDEX person_coverage_shot (source_id=?)
1298 1285  SEARCH s USING INDEX sqlite_autoindex_segments_1 (id=?)
1310 1285  CORRELATED SCALAR SUBQUERY 6
1314 1310  SEARCH e USING COVERING INDEX person_evidence_shot (source_id=? AND shot_id=?)
1336 1285  USE TEMP B-TREE FOR DISTINCT
1346  945  SEARCH e USING INDEX person_evidence_shot (source_id=?)
1354  945  CORRELATED SCALAR SUBQUERY 11
1368 1354  SEARCH role USING INDEX sqlite_autoindex_person_entities_1 (id=?)
1376 1354  SEARCH actor USING INDEX sqlite_autoindex_person_entities_1 (id=?)
1384 1354  SEARCH ps USING INDEX sqlite_autoindex_person_sources_1 (source_id=?)
1389 1354  SCAN json_each EXISTS VIRTUAL TABLE INDEX 3:
1398 1354  SCAN json_each EXISTS VIRTUAL TABLE INDEX 3:
1415  945  SEARCH s USING INDEX sqlite_autoindex_segments_1 (id=?)
1427  945  SEARCH a USING INDEX sqlite_autoindex_person_entities_1 (id=?)
1453  945  SEARCH k USING AUTOMATIC PARTIAL COVERING INDEX (source_id=? AND shot_id=?)
1500  945  USE TEMP B-TREE FOR LAST 5 TERMS OF ORDER BY
1530  942  SCAN (subquery-23)
1599  539  SEARCH s USING COVERING INDEX segments_timeline (source_id=?)
1607  539  BLOOM FILTER ON k (source_id=? AND shot_id=? AND accepted=?)
1632  539  SEARCH k USING AUTOMATIC PARTIAL COVERING INDEX (source_id=? AND shot_id=? AND accepted=?)
1648  539  BLOOM FILTER ON r (source_id=? AND shot_id=? AND rank=?)
1664  539  SEARCH r USING AUTOMATIC PARTIAL COVERING INDEX (source_id=? AND shot_id=? AND rank=?) LEFT-JOIN
1685    3  UNION ALL
1687 1685  CO-ROUTINE k
1688 1687  COMPOUND QUERY
1689 1688  LEFT-MOST SUBQUERY
1692 1689  MATERIALIZE valid_pairs
1702 1692  SCAN ps USING INDEX sqlite_autoindex_person_sources_1
1717 1692  SEARCH role USING AUTOMATIC PARTIAL COVERING INDEX (kind=?)
1722 1692  CORRELATED SCALAR SUBQUERY 1
1726 1722  SCAN json_each VIRTUAL TABLE INDEX 3:
1740 1692  SCAN performer VIRTUAL TABLE INDEX 3:
1746 1692  SEARCH actor USING INDEX sqlite_autoindex_person_entities_1 (id=?)
1766 1692  USE TEMP B-TREE FOR DISTINCT
1770 1689  MATERIALIZE marks
1778 1770  SCAN person_evidence USING INDEX person_evidence_shot
1817 1689  MATERIALIZE removed
1825 1817  SCAN person_coverage USING INDEX person_coverage_shot
1868 1689  SEARCH e USING INDEX person_evidence_shot (source_id=?)
1873 1689  SEARCH s USING INDEX sqlite_autoindex_segments_1 (id=?)
1887 1689  BLOOM FILTER ON pair (source_id=? AND role_id=? AND actor_id=?)
1898 1689  SEARCH pair USING AUTOMATIC COVERING INDEX (source_id=? AND role_id=? AND actor_id=?) LEFT-JOIN
1920 1689  SEARCH marks USING AUTOMATIC COVERING INDEX (source_id=? AND shot_id=?) LEFT-JOIN
1938 1689  SEARCH removed USING AUTOMATIC COVERING INDEX (source_id=?) LEFT-JOIN
2026 1688  UNION ALL
2034 2026  SEARCH c USING COVERING INDEX person_coverage_shot (source_id=?)
2040 2026  SEARCH s USING INDEX sqlite_autoindex_segments_1 (id=?)
2052 2026  CORRELATED SCALAR SUBQUERY 6
2056 2052  SEARCH e USING COVERING INDEX person_evidence_shot (source_id=? AND shot_id=?)
2078 2026  USE TEMP B-TREE FOR DISTINCT
2083 1685  SEARCH s USING COVERING INDEX segments_timeline (source_id=?)
2090 1685  CORRELATED SCALAR SUBQUERY 20
2104 2090  SEARCH c USING AUTOMATIC COVERING INDEX (source_id=? AND shot_id=?)
2135 1685  SEARCH k USING AUTOMATIC PARTIAL COVERING INDEX (source_id=? AND shot_id=?)
2147 1685  BLOOM FILTER ON r (source_id=? AND shot_id=? AND rank=?)
2161 1685  SEARCH r USING AUTOMATIC PARTIAL COVERING INDEX (source_id=? AND shot_id=? AND rank=?) LEFT-JOIN
2187    0  SCAN memberships
2190    0  USE TEMP B-TREE FOR GROUP BY
```
**查询 2**（4,135 字符）
```
   2    0  CO-ROUTINE k
   3    2  COMPOUND QUERY
   4    3  LEFT-MOST SUBQUERY
   7    4  MATERIALIZE valid_pairs
  17    7  SCAN ps USING INDEX sqlite_autoindex_person_sources_1
  32    7  SEARCH role USING AUTOMATIC PARTIAL COVERING INDEX (kind=?)
  37    7  CORRELATED SCALAR SUBQUERY 1
  41   37  SCAN json_each VIRTUAL TABLE INDEX 3:
  55    7  SCAN performer VIRTUAL TABLE INDEX 3:
  61    7  SEARCH actor USING INDEX sqlite_autoindex_person_entities_1 (id=?)
  81    7  USE TEMP B-TREE FOR DISTINCT
  85    4  MATERIALIZE marks
  93   85  SCAN person_evidence USING INDEX person_evidence_shot
 132    4  MATERIALIZE removed
 140  132  SCAN person_coverage USING INDEX person_coverage_shot
 183    4  SEARCH e USING INDEX person_evidence_shot (source_id=?)
 188    4  SEARCH s USING INDEX sqlite_autoindex_segments_1 (id=?)
 202    4  BLOOM FILTER ON pair (source_id=? AND role_id=? AND actor_id=?)
 213    4  SEARCH pair USING AUTOMATIC COVERING INDEX (source_id=? AND role_id=? AND actor_id=?) LEFT-JOIN
 235    4  SEARCH marks USING AUTOMATIC COVERING INDEX (source_id=? AND shot_id=?) LEFT-JOIN
 253    4  SEARCH removed USING AUTOMATIC COVERING INDEX (source_id=?) LEFT-JOIN
 340    3  UNION ALL
 347  340  SEARCH c USING COVERING INDEX person_coverage_shot (source_id=?)
 353  340  SEARCH s USING INDEX sqlite_autoindex_segments_1 (id=?)
 365  340  CORRELATED SCALAR SUBQUERY 6
 369  365  SEARCH e USING COVERING INDEX person_evidence_shot (source_id=? AND shot_id=?)
 391  340  USE TEMP B-TREE FOR DISTINCT
 395    0  MATERIALIZE ranked
 398  395  CO-ROUTINE (subquery-23)
 400  398  CO-ROUTINE k
 401  400  COMPOUND QUERY
 402  401  LEFT-MOST SUBQUERY
 405  402  MATERIALIZE valid_pairs
 415  405  SCAN ps USING INDEX sqlite_autoindex_person_sources_1
 430  405  SEARCH role USING AUTOMATIC PARTIAL COVERING INDEX (kind=?)
 435  405  CORRELATED SCALAR SUBQUERY 1
 439  435  SCAN json_each VIRTUAL TABLE INDEX 3:
 453  405  SCAN performer VIRTUAL TABLE INDEX 3:
 459  405  SEARCH actor USING INDEX sqlite_autoindex_person_entities_1 (id=?)
 479  405  USE TEMP B-TREE FOR DISTINCT
 483  402  MATERIALIZE marks
 491  483  SCAN person_evidence USING INDEX person_evidence_shot
 530  402  MATERIALIZE removed
 538  530  SCAN person_coverage USING INDEX person_coverage_shot
 581  402  SEARCH e USING INDEX person_evidence_shot (source_id=?)
 586  402  SEARCH s USING INDEX sqlite_autoindex_segments_1 (id=?)
 600  402  BLOOM FILTER ON pair (source_id=? AND role_id=? AND actor_id=?)
 611  402  SEARCH pair USING AUTOMATIC COVERING INDEX (source_id=? AND role_id=? AND actor_id=?) LEFT-JOIN
 633  402  SEARCH marks USING AUTOMATIC COVERING INDEX (source_id=? AND shot_id=?) LEFT-JOIN
 651  402  SEARCH removed USING AUTOMATIC COVERING INDEX (source_id=?) LEFT-JOIN
 738  401  UNION ALL
 745  738  SEARCH c USING COVERING INDEX person_coverage_shot (source_id=?)
 751  738  SEARCH s USING INDEX sqlite_autoindex_segments_1 (id=?)
 763  738  CORRELATED SCALAR SUBQUERY 6
 767  763  SEARCH e USING COVERING INDEX person_evidence_shot (source_id=? AND shot_id=?)
 789  738  USE TEMP B-TREE FOR DISTINCT
 799  398  SEARCH e USING INDEX person_evidence_shot (source_id=?)
 807  398  CORRELATED SCALAR SUBQUERY 11
 821  807  SEARCH role USING INDEX sqlite_autoindex_person_entities_1 (id=?)
 829  807  SEARCH actor USING INDEX sqlite_autoindex_person_entities_1 (id=?)
 837  807  SEARCH ps USING INDEX sqlite_autoindex_person_sources_1 (source_id=?)
 842  807  SCAN json_each EXISTS VIRTUAL TABLE INDEX 3:
 851  807  SCAN json_each EXISTS VIRTUAL TABLE INDEX 3:
 868  398  SEARCH s USING INDEX sqlite_autoindex_segments_1 (id=?)
 880  398  SEARCH a USING INDEX sqlite_autoindex_person_entities_1 (id=?)
 906  398  SEARCH k USING AUTOMATIC PARTIAL COVERING INDEX (source_id=? AND shot_id=?)
 953  398  USE TEMP B-TREE FOR LAST 5 TERMS OF ORDER BY
 983  395  SCAN (subquery-23)
1051    0  SEARCH s USING COVERING INDEX segments_timeline (source_id=?)
1075    0  SEARCH k USING AUTOMATIC PARTIAL COVERING INDEX (source_id=? AND shot_id=?)
1087    0  BLOOM FILTER ON r (source_id=? AND shot_id=? AND rank=?)
1101    0  SEARCH r USING AUTOMATIC PARTIAL COVERING INDEX (source_id=? AND shot_id=? AND rank=?) LEFT-JOIN
```
**查询 3**（4,410 字符）
```
   2    0  CO-ROUTINE k
   3    2  COMPOUND QUERY
   4    3  LEFT-MOST SUBQUERY
   7    4  MATERIALIZE valid_pairs
  17    7  SCAN ps USING INDEX sqlite_autoindex_person_sources_1
  32    7  SEARCH role USING AUTOMATIC PARTIAL COVERING INDEX (kind=?)
  37    7  CORRELATED SCALAR SUBQUERY 1
  41   37  SCAN json_each VIRTUAL TABLE INDEX 3:
  55    7  SCAN performer VIRTUAL TABLE INDEX 3:
  61    7  SEARCH actor USING INDEX sqlite_autoindex_person_entities_1 (id=?)
  81    7  USE TEMP B-TREE FOR DISTINCT
  85    4  MATERIALIZE marks
  93   85  SCAN person_evidence USING INDEX person_evidence_shot
 132    4  MATERIALIZE removed
 140  132  SCAN person_coverage USING INDEX person_coverage_shot
 183    4  SEARCH e USING INDEX person_evidence_shot (source_id=?)
 188    4  SEARCH s USING INDEX sqlite_autoindex_segments_1 (id=?)
 202    4  BLOOM FILTER ON pair (source_id=? AND role_id=? AND actor_id=?)
 213    4  SEARCH pair USING AUTOMATIC COVERING INDEX (source_id=? AND role_id=? AND actor_id=?) LEFT-JOIN
 235    4  SEARCH marks USING AUTOMATIC COVERING INDEX (source_id=? AND shot_id=?) LEFT-JOIN
 253    4  SEARCH removed USING AUTOMATIC COVERING INDEX (source_id=?) LEFT-JOIN
 340    3  UNION ALL
 347  340  SEARCH c USING COVERING INDEX person_coverage_shot (source_id=?)
 353  340  SEARCH s USING INDEX sqlite_autoindex_segments_1 (id=?)
 365  340  CORRELATED SCALAR SUBQUERY 6
 369  365  SEARCH e USING COVERING INDEX person_evidence_shot (source_id=? AND shot_id=?)
 391  340  USE TEMP B-TREE FOR DISTINCT
 395    0  MATERIALIZE ranked
 398  395  CO-ROUTINE (subquery-25)
 400  398  CO-ROUTINE k
 401  400  COMPOUND QUERY
 402  401  LEFT-MOST SUBQUERY
 405  402  MATERIALIZE valid_pairs
 415  405  SCAN ps USING INDEX sqlite_autoindex_person_sources_1
 430  405  SEARCH role USING AUTOMATIC PARTIAL COVERING INDEX (kind=?)
 435  405  CORRELATED SCALAR SUBQUERY 1
 439  435  SCAN json_each VIRTUAL TABLE INDEX 3:
 453  405  SCAN performer VIRTUAL TABLE INDEX 3:
 459  405  SEARCH actor USING INDEX sqlite_autoindex_person_entities_1 (id=?)
 479  405  USE TEMP B-TREE FOR DISTINCT
 483  402  MATERIALIZE marks
 491  483  SCAN person_evidence USING INDEX person_evidence_shot
 530  402  MATERIALIZE removed
 538  530  SCAN person_coverage USING INDEX person_coverage_shot
 581  402  SEARCH e USING INDEX person_evidence_shot (source_id=?)
 586  402  SEARCH s USING INDEX sqlite_autoindex_segments_1 (id=?)
 600  402  BLOOM FILTER ON pair (source_id=? AND role_id=? AND actor_id=?)
 611  402  SEARCH pair USING AUTOMATIC COVERING INDEX (source_id=? AND role_id=? AND actor_id=?) LEFT-JOIN
 633  402  SEARCH marks USING AUTOMATIC COVERING INDEX (source_id=? AND shot_id=?) LEFT-JOIN
 651  402  SEARCH removed USING AUTOMATIC COVERING INDEX (source_id=?) LEFT-JOIN
 738  401  UNION ALL
 745  738  SEARCH c USING COVERING INDEX person_coverage_shot (source_id=?)
 751  738  SEARCH s USING INDEX sqlite_autoindex_segments_1 (id=?)
 763  738  CORRELATED SCALAR SUBQUERY 6
 767  763  SEARCH e USING COVERING INDEX person_evidence_shot (source_id=? AND shot_id=?)
 789  738  USE TEMP B-TREE FOR DISTINCT
 799  398  SEARCH e USING INDEX person_evidence_shot (source_id=?)
 807  398  CORRELATED SCALAR SUBQUERY 11
 821  807  SEARCH role USING INDEX sqlite_autoindex_person_entities_1 (id=?)
 829  807  SEARCH actor USING INDEX sqlite_autoindex_person_entities_1 (id=?)
 837  807  SEARCH ps USING INDEX sqlite_autoindex_person_sources_1 (source_id=?)
 842  807  SCAN json_each EXISTS VIRTUAL TABLE INDEX 3:
 851  807  SCAN json_each EXISTS VIRTUAL TABLE INDEX 3:
 868  398  SEARCH s USING INDEX sqlite_autoindex_segments_1 (id=?)
 880  398  SEARCH a USING INDEX sqlite_autoindex_person_entities_1 (id=?)
 906  398  SEARCH k USING AUTOMATIC PARTIAL COVERING INDEX (source_id=? AND shot_id=?)
 953  398  USE TEMP B-TREE FOR LAST 5 TERMS OF ORDER BY
 983  395  SCAN (subquery-25)
1056    0  SCAN k
1063    0  SEARCH s USING INDEX sqlite_autoindex_segments_1 (id=?)
1077    0  BLOOM FILTER ON r (source_id=? AND shot_id=? AND rank=?)
1094    0  SEARCH r USING AUTOMATIC PARTIAL COVERING INDEX (source_id=? AND shot_id=? AND rank=?) LEFT-JOIN
1122    0  CORRELATED SCALAR SUBQUERY 23
1125 1122  CO-ROUTINE (subquery-22)
1127 1125  CO-ROUTINE confirmed
1129 1127  CO-ROUTINE k
1130 1129  COMPOUND QUERY
1131 1130  LEFT-MOST SUBQUERY
1134 1131  MATERIALIZE valid_pairs
1144 1134  SCAN ps USING INDEX sqlite_autoindex_person_sources_1
1159 1134  SEARCH role USING AUTOMATIC PARTIAL COVERING INDEX (kind=?)
1164 1134  CORRELATED SCALAR SUBQUERY 1
1168 1164  SCAN json_each VIRTUAL TABLE INDEX 3:
1182 1134  SCAN performer VIRTUAL TABLE INDEX 3:
1188 1134  SEARCH actor USING INDEX sqlite_autoindex_person_entities_1 (id=?)
1208 1134  USE TEMP B-TREE FOR DISTINCT
1212 1131  MATERIALIZE marks
1220 1212  SCAN person_evidence USING INDEX person_evidence_shot
1259 1131  MATERIALIZE removed
1267 1259  SCAN person_coverage USING INDEX person_coverage_shot
1310 1131  SEARCH e USING INDEX person_evidence_shot (source_id=?)
1315 1131  SEARCH s USING INDEX sqlite_autoindex_segments_1 (id=?)
1329 1131  BLOOM FILTER ON pair (source_id=? AND role_id=? AND actor_id=?)
1340 1131  SEARCH pair USING AUTOMATIC COVERING INDEX (source_id=? AND role_id=? AND actor_id=?) LEFT-JOIN
1362 1131  SEARCH marks USING AUTOMATIC COVERING INDEX (source_id=? AND shot_id=?) LEFT-JOIN
1380 1131  SEARCH removed USING AUTOMATIC COVERING INDEX (source_id=?) LEFT-JOIN
1467 1130  UNION ALL
1474 1467  SEARCH c USING COVERING INDEX person_coverage_shot (source_id=?)
1480 1467  SEARCH s USING INDEX sqlite_autoindex_segments_1 (id=?)
1492 1467  CORRELATED SCALAR SUBQUERY 6
1496 1492  SEARCH e USING COVERING INDEX person_evidence_shot (source_id=? AND shot_id=?)
1518 1467  USE TEMP B-TREE FOR DISTINCT
1525 1127  SEARCH e USING INDEX person_evidence_shot (source_id=?)
1535 1127  CORRELATED SCALAR SUBQUERY 16
1549 1535  SEARCH role USING INDEX sqlite_autoindex_person_entities_1 (id=?)
1557 1535  SEARCH actor USING INDEX sqlite_autoindex_person_entities_1 (id=?)
1565 1535  SEARCH ps USING INDEX sqlite_autoindex_person_sources_1 (source_id=?)
1570 1535  SCAN json_each EXISTS VIRTUAL TABLE INDEX 3:
1579 1535  SCAN json_each EXISTS VIRTUAL TABLE INDEX 3:
1596 1127  SEARCH s USING INDEX sqlite_autoindex_segments_1 (id=?)
1626 1127  SEARCH k USING AUTOMATIC PARTIAL COVERING INDEX (source_id=? AND shot_id=?)
1644 1127  USE TEMP B-TREE FOR DISTINCT
1660 1125  SEARCH c USING AUTOMATIC COVERING INDEX (source_id=? AND shot_id=?)
1671 1125  USE TEMP B-TREE FOR ORDER BY
1682 1122  SCAN (subquery-22)
1702    0  USE TEMP B-TREE FOR ORDER BY
```

## 证据图片 `GET /api/persons/evidence/{id}/image`

| 抽样 | 请求数 | 分支分布 | p50 / p95 ms | versions_scan p50 ms |
| --- | --- | --- | --- | --- |
| 有 image_path | 10 | {'current_path': 10} | 3 / 8 | - |
| 无 image_path | 10 | {'shot_thumbnail': 10} | 939 / 1041 | 934 / 1035 |

只读校验：主库大小与修改时间未变化。
