// React Bits: Components/AnimatedList @ c49d6978d2496660f0f0c5a3b3ca77a059566a93
import { useRef, useState, useEffect, useCallback } from 'react';
import { motion, useInView } from 'motion/react';
import './AnimatedList.css';

const AnimatedItem = ({ children, delay = 0, index, onMouseEnter, onClick, selected, separator = false, id }) => {
  const ref = useRef(null);
  const inView = useInView(ref, { amount: 0.5, triggerOnce: false });
  return (
    <motion.div
      ref={ref}
      id={id} // MASON MOD M-AnimatedList-6: option id (a combobox input points aria-activedescendant at it)
      data-index={index}
      data-sep={separator ? '1' : undefined} // MASON MOD M-AnimatedList-5: a separator row is not an option
      role={separator ? 'presentation' : 'option'} // MASON MOD M-AnimatedList-2
      aria-selected={separator ? undefined : !!selected}
      onMouseEnter={separator ? undefined : onMouseEnter}
      onClick={separator ? undefined : onClick}
      initial={{ scale: 0.7, opacity: 0 }}
      animate={inView ? { scale: 1, opacity: 1 } : { scale: 0.7, opacity: 0 }}
      transition={{ duration: 0.2, delay }}
      style={{ marginBottom: '1rem', cursor: separator ? 'default' : 'pointer' }}
    >
      {children}
    </motion.div>
  );
};

const AnimatedList = ({
  items = [
    'Item 1',
    'Item 2',
    'Item 3',
    'Item 4',
    'Item 5',
    'Item 6',
    'Item 7',
    'Item 8',
    'Item 9',
    'Item 10',
    'Item 11',
    'Item 12',
    'Item 13',
    'Item 14',
    'Item 15'
  ],
  onItemSelect,
  showGradients = true,
  enableArrowNavigation = true,
  className = '',
  itemClassName = '',
  displayScrollbar = true,
  initialSelectedIndex = -1,
  // MASON MOD M-AnimatedList-1: rich rows + stable keys + a hard cap (no 10k-row freeze).
  renderItem, // (item, index, selected) => node; default renders the item as text like upstream
  itemKey, // (item, index) => key; default index (upstream)
  maxItems = 200,
  ariaLabel = 'List',
  onCap, // MASON MOD M-AnimatedList-3: ({shown, total}) when items were cut to maxItems
  capLabel, // MASON MOD M-AnimatedList-3: (shown, total) => node, or false to hide the note
  selectedIndex: selectedIndexProp, // MASON MOD M-AnimatedList-4: controlled highlight (number); omit = uncontrolled
  isSeparator, // MASON MOD M-AnimatedList-5: (item, index) => true for a heading row (not selectable, skipped by the arrows)
  listId, // MASON MOD M-AnimatedList-6: id of the listbox element (a combobox's aria-controls)
  optionId // MASON MOD M-AnimatedList-6: (item, index) => id of that option row (a combobox's aria-activedescendant)
}) => {
  const allItems = items;
  items = Array.isArray(allItems) ? allItems.slice(0, Math.max(0, maxItems)) : []; // MASON MOD M-AnimatedList-1
  const totalItems = Array.isArray(allItems) ? allItems.length : 0; // MASON MOD M-AnimatedList-3
  const capped = totalItems > items.length;
  const onCapRef = useRef(onCap);
  onCapRef.current = onCap;
  useEffect(() => {
    if (capped && typeof onCapRef.current === 'function') onCapRef.current({ shown: items.length, total: totalItems });
  }, [capped, items.length, totalItems]);
  const listRef = useRef(null);
  // MASON MOD M-AnimatedList-5: heading rows inside the list (the calendar's day headers).
  const hasSeps = typeof isSeparator === 'function';
  const isSep = index => hasSeps && !!isSeparator(items[index], index);
  const stepFrom = (prev, dir) => {
    let i = prev + dir;
    while (i >= 0 && i < items.length && isSep(i)) i += dir;
    return i < 0 || i >= items.length ? prev : i;
  };
  const [selectedIndexState, setSelectedIndex] = useState(initialSelectedIndex);
  // MASON MOD M-AnimatedList-4: when the parent passes a numeric selectedIndex (WM's palette via
  // MasonUI.mount(...).update({selectedIndex})) it wins over internal hover/arrow state, the rows stay
  // mounted (no entrance replay) and the selected row is scrolled into view.
  const controlled = typeof selectedIndexProp === 'number' && Number.isFinite(selectedIndexProp);
  const selectedIndex = controlled ? selectedIndexProp : selectedIndexState;
  useEffect(() => {
    if (controlled) setKeyboardNav(true);
  }, [controlled, selectedIndexProp]);
  const [keyboardNav, setKeyboardNav] = useState(false);
  const [topGradientOpacity, setTopGradientOpacity] = useState(0);
  const [bottomGradientOpacity, setBottomGradientOpacity] = useState(1);

  const handleItemMouseEnter = useCallback(index => {
    setSelectedIndex(index);
  }, []);

  const handleItemClick = useCallback(
    (item, index) => {
      setSelectedIndex(index);
      if (onItemSelect) {
        onItemSelect(item, index);
      }
    },
    [onItemSelect]
  );

  const handleScroll = useCallback(e => {
    const { scrollTop, scrollHeight, clientHeight } = e.target;
    setTopGradientOpacity(Math.min(scrollTop / 50, 1));
    const bottomDistance = scrollHeight - (scrollTop + clientHeight);
    setBottomGradientOpacity(scrollHeight <= clientHeight ? 0 : Math.min(bottomDistance / 50, 1));
  }, []);

  // MASON MOD M-AnimatedList-2: keys are handled on the focused list only (upstream listens on
  // window, so every list on the dashboard reacted to every arrow key), and Tab is no longer
  // trapped (keyboard users must be able to leave the list).
  useEffect(() => {
    if (!enableArrowNavigation) return;
    const el = listRef.current;
    if (!el) return;
    const handleKeyDown = e => {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setKeyboardNav(true);
        setSelectedIndex(prev => (hasSeps ? stepFrom(prev, 1) : Math.min(prev + 1, items.length - 1))); // MASON MOD M-AnimatedList-5
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        setKeyboardNav(true);
        setSelectedIndex(prev => (hasSeps ? stepFrom(prev, -1) : Math.max(prev - 1, 0))); // MASON MOD M-AnimatedList-5
      } else if (e.key === 'Enter') {
        if (selectedIndex >= 0 && selectedIndex < items.length) {
          e.preventDefault();
          if (onItemSelect) {
            onItemSelect(items[selectedIndex], selectedIndex);
          }
        }
      }
    };

    el.addEventListener('keydown', handleKeyDown);
    return () => el.removeEventListener('keydown', handleKeyDown);
  }, [items, selectedIndex, onItemSelect, enableArrowNavigation]);

  useEffect(() => {
    if (!keyboardNav || selectedIndex < 0 || !listRef.current) return;
    const container = listRef.current;
    const selectedItem = container.querySelector(`[data-index="${selectedIndex}"]`);
    if (selectedItem) {
      const extraMargin = 50;
      const containerScrollTop = container.scrollTop;
      const containerHeight = container.clientHeight;
      const itemTop = selectedItem.offsetTop;
      const itemBottom = itemTop + selectedItem.offsetHeight;
      if (itemTop < containerScrollTop + extraMargin) {
        container.scrollTo({ top: itemTop - extraMargin, behavior: 'smooth' });
      } else if (itemBottom > containerScrollTop + containerHeight - extraMargin) {
        container.scrollTo({
          top: itemBottom - containerHeight + extraMargin,
          behavior: 'smooth'
        });
      }
    }
    setKeyboardNav(false);
  }, [selectedIndex, keyboardNav]);

  return (
    <div className={`scroll-list-container ${className}`}>
      <div
        ref={listRef}
        id={listId} // MASON MOD M-AnimatedList-6
        className={`scroll-list ${!displayScrollbar ? 'no-scrollbar' : ''}`}
        onScroll={handleScroll}
        role="listbox" // MASON MOD M-AnimatedList-2
        aria-label={ariaLabel}
        tabIndex={enableArrowNavigation ? 0 : -1}
      >
        {items.map((item, index) =>
          isSep(index) ? ( // MASON MOD M-AnimatedList-5: heading row, no .item card, never selected
            <AnimatedItem key={itemKey ? itemKey(item, index) : index} delay={0.1} index={index} separator>
              <div className={`item-sep ${itemClassName}`}>{renderItem ? renderItem(item, index, false) : item}</div>
            </AnimatedItem>
          ) : (
          <AnimatedItem
            key={itemKey ? itemKey(item, index) : index}
            id={optionId ? optionId(item, index) : undefined} // MASON MOD M-AnimatedList-6
            delay={0.1}
            index={index}
            selected={selectedIndex === index}
            onMouseEnter={() => handleItemMouseEnter(index)}
            onClick={() => handleItemClick(item, index)}
          >
            <div className={`item ${selectedIndex === index ? 'selected' : ''} ${itemClassName}`}>
              {renderItem ? (
                <div className="item-text">{renderItem(item, index, selectedIndex === index)}</div>
              ) : (
                <p className="item-text">{item}</p>
              )}
            </div>
          </AnimatedItem>
          )
        )}
      </div>
      {capped && capLabel !== false ? ( // MASON MOD M-AnimatedList-3: the cap is never silent; outside the listbox
        <div className="mui-list-cap" role="note">
          {typeof capLabel === 'function'
            ? capLabel(items.length, totalItems)
            : `${items.length.toLocaleString('en-US')} of ${totalItems.toLocaleString('en-US')} shown`}
        </div>
      ) : null}
      {showGradients && (
        <>
          <div className="top-gradient" style={{ opacity: topGradientOpacity }}></div>
          <div className="bottom-gradient" style={{ opacity: bottomGradientOpacity }}></div>
        </>
      )}
    </div>
  );
};

export default AnimatedList;
