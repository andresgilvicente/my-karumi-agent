"""Workers: the peers that share Pipecat's bus.

`BrowserWorker` owns the page; `KarumiUIWorker` owns the reasoning about it.
Splitting them is what lets the model be swapped, resized or removed entirely
without touching the code that drives the browser.
"""
