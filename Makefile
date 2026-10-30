.PHONY: test shadow paper stocks review review-claude inbox kill

test:            ## run the offline test suite
	python -m unittest discover -s tests -v

shadow:          ## one cycle, no paper fill; queues tokens when the analyst is external
	python run_crypto.py --shadow --once

inbox:           ## list tokens waiting on the external analyst
	python analyst_inbox.py list

paper:           ## paper trading, every 60 seconds
	python run_crypto.py

stocks:          ## Alpaca paper account, every 15 minutes while open
	python run_stocks.py

review:          ## weekly stats from journal.jsonl
	python review.py

review-claude:   ## weekly stats + a blunt written review
	python review.py --claude

kill:            ## stop the desk on its next cycle
	touch KILL
