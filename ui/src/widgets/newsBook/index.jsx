// newsBook — "For your book" (News page, UI overhaul Task 5): stories touching holdings + watchlist,
// with Mason's implication line. Shares the page's news store (newsShared/store.js) with the other
// five News widgets, so the HUD chips, a bubble or a timeline scrub narrows this list too.
import Feed from '../newsShared/Feed.jsx';
import '../newsShared/news.css';

export default function NewsBook() {
  return <Feed which="book" />;
}
