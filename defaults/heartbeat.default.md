Heartbeat check-in. Review the watchlist and every open position:
1. Fetch quotes for the watchlist and the held symbols.
2. For held positions, check technicals (RSI, trend vs. 50/200-day averages) and recent headlines.

Only set notify=true when something genuinely needs the user's attention, for example:
- a held position moved more than 3% since the previous close,
- RSI crossed below 30 or above 70 on a held position,
- a headline that plausibly changes the thesis for a held position,
- an unpushed commit is waiting whose prices have moved more than 2%.

Otherwise set notify=false and keep the headline short. Never stage or commit orders during a heartbeat.
