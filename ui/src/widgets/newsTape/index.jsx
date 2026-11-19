// newsTape — "The tape" (News page, UI overhaul Task 5): macro + market stories, newest first, the
// first 12 then "+N more". Shares the page's news store with the other News widgets.
import Feed from '../newsShared/Feed.jsx';
import '../newsShared/news.css';

export default function NewsTape() {
  return <Feed which="tape" />;
}
