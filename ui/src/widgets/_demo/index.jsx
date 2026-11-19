// F0 pipeline demo widget (see meta.js). Shows the conventions F1–F5 follow:
//  - data only through useRhApi(route) + <RhState> (loading / Off / error / stale badge)
//  - per-instance state lives in the component (two instances never share it)
//  - strings render through JSX only; numbers through lib/fmt.js (sign + ▲/▼, n=)
import { useState } from 'react';
import AnimatedList from '../../reactbits/AnimatedList/AnimatedList.jsx';
import CountUp from '../../reactbits/CountUp/CountUp.jsx';
import ShinyText from '../../reactbits/ShinyText/ShinyText.jsx';
import { useRhApi } from '../../lib/api.js';
import { RhState } from '../../lib/states.jsx';
import { pct, arrow, dirClass, dateTime } from '../../lib/fmt.js';
import './demo.css';

function Row({ r }) {
  return (
    <div className="mui-demo__row">
      <b className="mui-demo__tk">{r.ticker}</b>
      <span className="mui-demo__what">{r.what}</span>
      <span className={dirClass(r.chg_pct)}>
        {arrow(r.chg_pct)} {pct(r.chg_pct)}
      </span>
      <span className="mui-n">{dateTime(r.at, { utc: true })}</span>
    </div>
  );
}

export default function DemoWidget({ config, saveConfig }) {
  const api = useRhApi('/api/_demo/feed');
  const [count, setCount] = useState(0);
  if (config.throwOnRender) throw new Error('demo crash requested by config');
  return (
    <div className="mui-demo">
      <div className="mui-demo__head">
        <ShinyText text={String(config.label)} speed={3} color="#8b96b8" shineColor="#e8edf7" />
        <button type="button" className="mui-btn mui-btn--ghost" data-testid="demo-bump" onClick={() => setCount(c => c + 1)}>
          +1
        </button>
        <span data-testid="demo-count">{count}</span>
        <button type="button" className="mui-btn mui-btn--ghost" data-testid="demo-rename" onClick={() => saveConfig({ label: `${config.label}!` })}>
          rename
        </button>
      </div>
      <RhState api={api} empty={d => !d || !Array.isArray(d.rows) || d.rows.length === 0} emptyText="No rows.">
        {d => (
          <>
            <div className="mui-demo__stat">
              <CountUp to={d.rows.length} duration={1} /> rows
            </div>
            <div className="mui-demo__list">
              <AnimatedList
                items={d.rows}
                itemKey={r => r.id}
                renderItem={r => <Row r={r} />}
                showGradients
                enableArrowNavigation
                ariaLabel="Demo rows"
              />
            </div>
          </>
        )}
      </RhState>
    </div>
  );
}
