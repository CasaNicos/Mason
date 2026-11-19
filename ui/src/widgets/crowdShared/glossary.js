// Plain-English glossary for the #crowd page (UI overhaul Task 4). <Term k="ci"> shows one of these on
// hover / keyboard focus / tap. Keep each entry to one or two short sentences a beginner can read at a glance.
export const GLOSSARY = {
  popular: {
    title: '100 Most Popular',
    body: "Robinhood's public list of the 100 stocks and funds held by the most Robinhood customers. Mason saves a copy once a day after the market closes."
  },
  crowd: {
    title: 'The crowd',
    body: "Shorthand for Robinhood's everyday investors, seen through what they collectively own: the 100 Most Popular list."
  },
  rank: {
    title: 'Rank',
    body: 'Position on the list. #1 is owned by the most Robinhood customers; a smaller number means more popular.'
  },
  climb: {
    title: 'Climbing / slipping',
    body: '▲ = moved up the list since the previous daily snapshot (more popular); ▼ = moved down.'
  },
  enter: {
    title: 'Joined ◆',
    body: "A stock that was not on the previous day's list shows up on today's. Mason grades what it did next."
  },
  exit: {
    title: 'Left ◇',
    body: 'A stock missing from the list two snapshots in a row. A single missing day is treated as noise, not an exit.'
  },
  spy: {
    title: 'vs SPY',
    body: 'Compared with SPY, a fund that tracks the S&P 500 (the broad market) over the same days. "+2% vs SPY" means it beat the market by 2 percentage points.'
  },
  sessions: {
    title: 'Trading sessions',
    body: 'Market days. 5 sessions is about a week, 20 about a month, 60 about three months.'
  },
  n: {
    title: 'n',
    body: 'How many examples a number is based on. With a small n, luck can easily look like a pattern.'
  },
  ci: {
    title: '95% CI (likely range)',
    body: 'The range where the true average most likely sits. If the range crosses 0, the result could still be luck.'
  },
  hit: {
    title: 'Beat SPY',
    body: 'The share of examples that did better than SPY over that window.'
  },
  verdict: {
    title: 'Verdict',
    body: '"Supported" or "Contradicted" only once the likely range sits fully on one side of zero; otherwise "Inconclusive".'
  },
  held: {
    title: 'Yours',
    body: '● held = in your Robinhood accounts. ○ watched = on your Mason watchlist.'
  },
  activity: {
    title: 'Crowd activity',
    body: 'How much the list reshuffled since the previous daily snapshot: names that climbed or slipped 3+ places, joined or left.'
  },
  cap: {
    title: 'Market cap',
    body: 'Company size: share price × all shares. Mega ≥ $200B, large ≥ $10B, mid ≥ $2B, small ≥ $300M.'
  },
  etf: {
    title: 'Fund (ETF)',
    body: 'An exchange-traded fund holds many stocks at once and trades like a single stock (VOO and SPY are ETFs).'
  },
  pe: {
    title: 'P/E ratio',
    body: 'Share price ÷ yearly profit per share. Higher means investors pay more for each dollar the company earns.'
  },
  yield: {
    title: 'Dividend yield',
    body: 'Yearly cash dividends as a percentage of the share price.'
  },
  range52: {
    title: '52-week range',
    body: "The lowest and highest share price over the last year."
  }
};

export const termOf = k => (Object.prototype.hasOwnProperty.call(GLOSSARY, k) ? GLOSSARY[k] : null);
