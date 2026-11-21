// Plain-language explanations for every piece of jargon shown in the UI.
export const GLOSSARY = {
  pop: { name: "Chance of profit (POP)", def: "The model's estimate of how often this trade finishes with any profit at expiry. An estimate, not a promise." },
  ev: { name: "Expected value (EV)", def: "Average profit or loss per contract-set if this exact trade could be repeated many times, after trading costs, under the model's view. Positive is good, but it is still an estimate." },
  max_loss: { name: "Max loss", def: "The most this trade can lose per contract-set, no matter what the stock does. All trades here are 'defined risk', so this is known up front." },
  max_profit: { name: "Max profit", def: "The most this trade can make per contract-set. 'Unlimited' means there is no cap in theory." },
  breakeven: { name: "Breakeven", def: "The stock price at expiry where the trade neither makes nor loses money." },
  iv: { name: "Implied volatility (IV)", def: "How much movement the options market is pricing in, as a yearly percentage. Higher IV = more expensive options." },
  rv: { name: "Forecast volatility", def: "How much the stock is likely to actually move, according to Kwant Trades's statistical models (GARCH, HAR, EWMA and others blended)." },
  vol_edge: { name: "Vol edge", def: "Forecast volatility compared with implied volatility. Positive: options look cheap compared with how much we expect the stock to move. Negative: options look expensive (rich)." },
  cheap: { name: "Cheap", def: "This option's price is below what the fitted volatility curve says is fair, beyond the bid/ask spread. Buying cheap options is preferred." },
  rich: { name: "Rich", def: "This option's price is above what the fitted volatility curve says is fair, beyond the bid/ask spread. Selling rich options is preferred." },
  opening_range: { name: "Opening range", def: "The high and low of the first 15 minutes of trading (09:30–09:45 New York). A close above or below it is a classic day-trading signal." },
  vwap: { name: "VWAP", def: "Volume-weighted average price for today. Trading above it is mildly bullish, below it mildly bearish." },
  gap: { name: "Gap", def: "How far the stock opened from yesterday's close. Small gaps tend to continue; very large gaps often fade." },
  news: { name: "News sentiment", def: "Recent headlines scored from bearish (−1) to bullish (+1), weighted by relevance and how recent they are." },
  regime: { name: "Market regime", def: "The overall market backdrop: S&P 500 trend and fear gauge (VIX). In a stressed regime Kwant Trades automatically trades smaller." },
  risk_multiplier: { name: "Risk multiplier", def: "Scales every position size. 1.0 = normal size; 0.25 = quarter size because the market looks stressed." },
  composite: { name: "Composite direction", def: "All the signals blended into one score from −1 (bearish) to +1 (bullish)." },
  confidence: { name: "Confidence", def: "How strong and consistent the evidence is, from 0 to 100%." },
  agreement: { name: "Agreement", def: "Share of the signal weight that points the same way as the overall direction." },
  credit_spread: { name: "Credit spread", def: "Sell one option and buy a further-away one as protection. You collect cash up front; the bought option caps the loss." },
  debit_spread: { name: "Debit spread", def: "Buy one option and sell a further-away one to cut the cost. Cheaper than buying alone, but profit is capped." },
  smile: { name: "Volatility smile", def: "Implied volatility plotted across strike prices. It usually curves upward at the edges, like a smile or a smirk." },
  svi: { name: "SVI fit", def: "A standard smooth curve fitted through the market's implied volatilities. Points far from the curve may be mispriced." },
  zscore: { name: "Z-score", def: "How far the market price is from the fitted fair value, measured in units of the bid/ask spread. Above 2 is notable." },
  edge_after_costs: { name: "Edge after costs", def: "The estimated mispricing per share after paying to cross the bid/ask spread. Zero means it isn't tradable." },
  kill_switch: { name: "Kill switch", def: "Trips when the daily loss limit is hit: no new trades and open positions are closed." },
  open_risk: { name: "Open risk", def: "The total worst-case loss of everything you currently hold, compared with the cap you set." },
  daily_loss: { name: "Daily loss limit", def: "If today's losses reach this amount the kill switch trips and trading stops for the day." },
  delta: { name: "Delta", def: "How much the trade gains (per contract-set) for each $1 rise in the stock." },
  theta: { name: "Theta", def: "How much the trade gains or loses per day from time passing alone." },
  vega: { name: "Vega", def: "How much the trade gains for each 1 percentage-point rise in implied volatility." },
  gamma: { name: "Gamma", def: "How fast delta changes as the stock moves." },
  contracts: { name: "Contracts", def: "How many copies of the trade. One option contract covers 100 shares." },
  entry_limit: { name: "Limit price", def: "The per-share price Kwant Trades would offer. It will not pay more (or accept less) than this." },
  score: { name: "Score", def: "Ranking number: expected value per dollar risked, times confidence, times the regime multiplier. Higher ranks first." },
  win_rate: { name: "Win rate", def: "Share of trades that closed with a profit." },
  profit_factor: { name: "Profit factor", def: "Total gains divided by total losses. Above 1 means winners outweighed losers." },
  expectancy: { name: "Expectancy", def: "Average profit or loss per trade." },
  sharpe: { name: "Sharpe ratio", def: "Return divided by its day-to-day wobble. Higher is smoother; short backtests make it unreliable." },
  max_drawdown: { name: "Max drawdown", def: "Biggest drop from a peak to a later low in the equity curve." },
  shadow: { name: "Shadow mode", def: "Runs the full system and records the orders it WOULD send, but sends nothing. No money, not even practice money." },
  paper: { name: "Paper mode", def: "Trades practice money in Kwant Trades's built-in simulator. No real money is used." },
  alpaca_paper: { name: "Alpaca paper", def: "Sends orders to an Alpaca paper (practice) account. No real money is used." },
  scan: { name: "Scan", def: "Looks at every symbol once and ranks trade ideas. Never places orders." },
  data_delay: { name: "Data delay", def: "Free market data can be delayed. Prices shown may not match your broker's live quotes." },
  model: { name: "Model", def: "Which test found it: 'svi_residual' = off the fitted curve; 'parity' = calls and puts disagree; 'butterfly'/'calendar' = prices that break basic no-arbitrage rules." },
  rel_volume: { name: "Relative volume", def: "Opening-range volume compared with a normal day. Above 1.2× gives a breakout more credibility." },
  out_of_sample: { name: "Fresh trades", def: "Trades opened after the most recent learning change. They show whether the change works on trades it was not fitted to." },
  ci: { name: "90% range", def: "The range the true average most likely lies in, given how much results vary. If even its low end is above zero, the edge is unlikely to be luck." },
  sigma: { name: "Normal day's move", def: "How far the stock usually moves in one day, according to option prices. A 2× move is unusual." },
};

export const STRATEGIES = {
  long_call: { label: "Bullish bet (buy a call)", what: "Profits if the stock rises above the breakeven. You can lose only what you paid." },
  long_put: { label: "Bearish bet (buy a put)", what: "Profits if the stock falls below the breakeven. You can lose only what you paid." },
  bull_call_spread: { label: "Bullish, capped (call debit spread)", what: "Pay a little to profit from a moderate rise. Both the gain and the loss are capped." },
  bear_put_spread: { label: "Bearish, capped (put debit spread)", what: "Pay a little to profit from a moderate fall. Both the gain and the loss are capped." },
  bull_put_spread: { label: "Get paid if it holds up (put credit spread)", what: "Collect cash now; keep it if the stock stays above the sold strike. Loss is capped by the bought put." },
  bear_call_spread: { label: "Get paid if it stays down (call credit spread)", what: "Collect cash now; keep it if the stock stays below the sold strike. Loss is capped by the bought call." },
  iron_condor: { label: "Get paid if it stays in a range (iron condor)", what: "Collect cash now; keep it if the stock stays between the two sold strikes. Loss is capped on both sides." },
  long_straddle: { label: "Bet on a big move (straddle)", what: "Profits if the stock moves a lot in either direction. Loses if it sits still." },
  long_strangle: { label: "Bet on a big move, cheaper (strangle)", what: "Like a straddle but with cheaper, further-away options: needs a bigger move." },
};

export function strategyInfo(key, apiLabel) {
  const s = STRATEGIES[key];
  return { label: (typeof apiLabel === "string" && apiLabel) || (s ? s.label : key || "Unknown strategy"), what: s ? s.what : "" };
}

export const EXIT_REASONS = {
  take_profit: "Hit profit target",
  stop_loss: "Hit stop-loss",
  signal_invalidated: "Signal reversed",
  time_flatten: "End-of-day close",
  dte_exit: "Closed before expiry",
  kill_switch: "Kill switch",
  manual: "Closed by you",
  expired: "Expired",
  flatten_all: "Flatten all",
};
export const CAUSES = {
  direction_wrong: "Direction was wrong",
  too_slow: "Right direction, but the move came too slowly (time decay)",
  vol_over: "The stock moved less than the model expected (volatility overestimated)",
  vol_under: "The stock moved more than the model expected (volatility underestimated)",
  stopped_recovered: "Stopped out, then it recovered",
  costs: "Costs ate the edge",
  gap: "Big overnight/news gap",
  unknown: "Not enough data to tell",
  win: "Win",
};

export const exitLabel = (r) => (r ? EXIT_REASONS[r] || String(r).replace(/_/g, " ") : "–");

export const COMPONENTS = [
  { key: "orb", name: "Opening range", term: "opening_range" },
  { key: "vwap", name: "VWAP", term: "vwap" },
  { key: "gap", name: "Gap", term: "gap" },
  { key: "news", name: "News", term: "news" },
  { key: "regime", name: "Regime", term: "regime" },
];

export const ORB_STATES = {
  waiting: "Waiting for a breakout",
  triggered: "Breakout triggered",
  failed: "Breakout failed",
  target_hit: "Target reached",
};

export const MODES = {
  idle: { label: "IDLE", desc: "Not running", practice: false },
  scan: { label: "SCAN", desc: "Looking only, no orders", practice: false },
  shadow: { label: "SHADOW", desc: "Recording what it would trade, no orders sent", practice: false },
  paper: { label: "PAPER", desc: "Practice money (built-in simulator)", practice: true },
  alpaca_paper: { label: "ALPACA PAPER", desc: "Practice money (Alpaca paper account)", practice: true },
};

export const MARKET_STATES = {
  pre: "Pre-market",
  opening_range: "Opening range forming",
  open: "Market open",
  closed: "Market closed",
};
