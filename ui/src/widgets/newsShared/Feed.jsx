// newsShared/Feed.jsx — the "For your book" and "The tape" feeds (UI overhaul Task 5).
// React Bits AnimatedList (keyed rows: a poll updates rows in place, only new rows enter; arrow keys +
// Enter; 200-row cap, MODS M-AnimatedList-1..3). A row click / Enter opens the story in the story card
// (newsFocus) — the source link lives there. New arrivals type in once (DecryptedText, parts.jsx).
import { useCallback, useState } from 'react';
import AnimatedList from '../../reactbits/AnimatedList/AnimatedList.jsx';
import { num } from '../../lib/fmt.js';
import { derive, focusItem, useNews } from './store.js';
import { NewsState, StoryRow, usePlayed } from './parts.jsx';
import { clockRange } from './util.js';

const COPY = {
  book: {
    noun: 'touching your book',
    empty: 'Nothing touching your book right now.',
    aria: 'Stories touching your holdings and watchlist. Enter opens the story card.',
    first: 60
  },
  tape: {
    noun: 'on the tape',
    empty: 'Quiet on the macro tape.',
    aria: 'Macro and market stories. Enter opens the story card.',
    first: 12
  }
};

function selText(sel) {
  const parts = [];
  if (sel.ticker) parts.push(sel.ticker);
  if (sel.sector) parts.push(sel.sector);
  if (sel.range) parts.push(clockRange(sel.range));
  return parts.join(' · ');
}

export default function Feed({ which }) {
  const c = COPY[which] || COPY.tape;
  const s = useNews();
  const d = derive(s);
  const items = which === 'book' ? d.book : d.tape;
  const played = usePlayed();
  const [shown, setShown] = useState(c.first);
  const list = items.slice(0, shown);
  const more = items.length - list.length;
  const focusKey = s.sel.focus ? s.sel.focus.key : null;
  const onSelect = useCallback(it => focusItem(it), []);
  const fresh = s.fresh;
  const renderItem = useCallback(
    it => (
      <div className={it.key === focusKey ? 'nw-focusmark is-on' : 'nw-focusmark'}>
        <StoryRow it={it} fresh={fresh.has(it.key)} played={played} showImpl={which === 'book' || it.material} />
      </div>
    ),
    [focusKey, fresh, played, which]
  );
  const st = selText(s.sel);
  const newHere = items.reduce((n, it) => n + (fresh.has(it.key) && !played.has(it.key) ? 1 : 0), 0);
  const emptyText = items.length ? null : d.selLoading ? 'Scanning the day…' : st ? `No stories ${c.noun} for ${st}.` : c.empty;

  return (
    <div className={`nw nw-feed nw-feed--${which}`}>
      <div className="nw-feed__head">
        <span className="nw-feed__count">
          <b>{num(items.length)}</b> {c.noun}
          {st ? <span className="nw-feed__sel"> · {st}</span> : null}
          {d.selOn && d.scopeTotal > d.scope.length ? (
            <span className="nw-feed__cap" title="The newest stories that match; narrow the window to see older ones">
              {' '}
              · newest {num(d.scope.length)} of {num(d.scopeTotal)} matches
            </span>
          ) : null}
        </span>
        {newHere ? <span className="nw-new nw-new--pill">+{newHere} new</span> : null}
        {d.selLoading ? <span className="nw-feed__busy">scanning the day…</span> : null}
      </div>
      <NewsState s={s} empty={emptyText}>
        <div className="nw-feed__list">
          <AnimatedList
            items={list}
            itemKey={it => it.key}
            onItemSelect={onSelect}
            ariaLabel={c.aria}
            renderItem={renderItem}
            capLabel={false}
          />
        </div>
        {more > 0 ? (
          <button type="button" className="nw-more" onClick={() => setShown(n => n + 60)}>
            + {num(more)} more {c.noun}
          </button>
        ) : null}
      </NewsState>
    </div>
  );
}
