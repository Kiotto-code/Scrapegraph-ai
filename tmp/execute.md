python scripts/company_job_scraper.py --sources-file /tmp/ppg_source.txt   --out tmp
/ppg_check.json --out-csv tmp/ppg_check.csv -
-headless  
;; --fallback-portal https://careers.ppg.com/us/en/search-results

```
python scripts/company_job_scraper.py --sources-file tmp/petronas_source.txt --out tmp/petronas_paginated.json --out-csv tmp/petronas_paginated.csv --headless --fallback-portal https://careers.ppg.com/us/en/search-results --max-jobs 100
```
```
python scripts/company_job_scraper.py --sources-file tmp/petronas_source.txt --out tmp/petronas_paginated.json --out-csv tmp/petronas_paginated.csv --headless --max-jobs 100
```
```
python scripts/company_job_scraper.py --sources-file tmp/ppg_source.txt --out tmp/ppg_paginated.json --out-csv tmp/ppg_paginated.csv --headless --max-jobs 100
```