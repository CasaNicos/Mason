// React Bits: Components/Stack @ c49d6978d2496660f0f0c5a3b3ca77a059566a93
import { motion, useMotionValue, useTransform } from 'motion/react';
import { useState, useEffect } from 'react';
import { useDocumentVisible } from '../../lib/visibility.js'; // MASON MOD M-Stack-1
import './Stack.css';

// MASON MOD M-Stack-2: a card element that carries a React `key` keeps that key as its stack id, so the same
// card stays the same motion element when the `cards` prop is re-ordered and springs to its new slot.
// Cards without a key get the upstream slot id (index + 1), exactly as before.
const stackId = (content, index) => (content && content.key != null ? `k:${content.key}` : index + 1);

function CardRotate({ children, onSendToBack, sensitivity, disableDrag = false }) {
  const x = useMotionValue(0);
  const y = useMotionValue(0);
  const rotateX = useTransform(y, [-100, 100], [60, -60]);
  const rotateY = useTransform(x, [-100, 100], [-60, 60]);

  function handleDragEnd(_, info) {
    if (Math.abs(info.offset.x) > sensitivity || Math.abs(info.offset.y) > sensitivity) {
      onSendToBack();
    } else {
      x.set(0);
      y.set(0);
    }
  }

  if (disableDrag) {
    return (
      <motion.div className="card-rotate-disabled" style={{ x: 0, y: 0 }}>
        {children}
      </motion.div>
    );
  }

  return (
    <motion.div
      className="card-rotate"
      style={{ x, y, rotateX, rotateY }}
      drag
      dragConstraints={{ top: 0, right: 0, bottom: 0, left: 0 }}
      dragElastic={0.6}
      whileTap={{ cursor: 'grabbing' }}
      onDragEnd={handleDragEnd}
    >
      {children}
    </motion.div>
  );
}

export default function Stack({
  onSendToBack, // MASON MOD M-Stack-2: optional, called with the card's stack id when a drag / click / autoplay sends it back
  randomRotation = false,
  sensitivity = 200,
  cards = [],
  animationConfig = { stiffness: 260, damping: 20 },
  sendToBackOnClick = false,
  autoplay = false,
  autoplayDelay = 3000,
  pauseOnHover = false,
  mobileClickOnly = false,
  mobileBreakpoint = 768
}) {
  const [isMobile, setIsMobile] = useState(false);
  const [isPaused, setIsPaused] = useState(false);

  useEffect(() => {
    const checkMobile = () => {
      setIsMobile(window.innerWidth < mobileBreakpoint);
    };

    checkMobile();
    window.addEventListener('resize', checkMobile);
    return () => window.removeEventListener('resize', checkMobile);
  }, [mobileBreakpoint]);

  const shouldDisableDrag = mobileClickOnly && isMobile;
  const shouldEnableClick = sendToBackOnClick || shouldDisableDrag;

  const [stack, setStack] = useState(() => {
    if (cards.length) {
      return cards.map((content, index) => ({ id: stackId(content, index), content })); // MASON MOD M-Stack-2
    } else {
      // MASON MOD M-Stack-3: upstream's 4 demo cards (photos loaded from images.unsplash.com) are removed, so an
      // empty `cards` shows an empty deck and the bundle holds no outside address.
      return [];
    }
  });

  useEffect(() => {
    if (cards.length) {
      setStack(cards.map((content, index) => ({ id: stackId(content, index), content }))); // MASON MOD M-Stack-2
    }
  }, [cards]);

  const sendToBack = id => {
    if (typeof onSendToBack === 'function') onSendToBack(id); // MASON MOD M-Stack-2
    setStack(prev => {
      const newStack = [...prev];
      const index = newStack.findIndex(card => card.id === id);
      const [card] = newStack.splice(index, 1);
      newStack.unshift(card);
      return newStack;
    });
  };

  const docVisible = useDocumentVisible(); // MASON MOD M-Stack-1
  useEffect(() => {
    if (autoplay && stack.length > 1 && !isPaused && docVisible) {
      const interval = setInterval(() => {
        const topCardId = stack[stack.length - 1].id;
        sendToBack(topCardId);
      }, autoplayDelay);

      return () => clearInterval(interval);
    }
  }, [autoplay, autoplayDelay, stack, isPaused, docVisible]);

  return (
    <div
      className="stack-container"
      onMouseEnter={() => pauseOnHover && setIsPaused(true)}
      onMouseLeave={() => pauseOnHover && setIsPaused(false)}
    >
      {stack.map((card, index) => {
        const randomRotate = randomRotation ? Math.random() * 10 - 5 : 0;
        return (
          <CardRotate
            key={card.id}
            onSendToBack={() => sendToBack(card.id)}
            sensitivity={sensitivity}
            disableDrag={shouldDisableDrag}
          >
            <motion.div
              className="card"
              onClick={() => shouldEnableClick && sendToBack(card.id)}
              animate={{
                rotateZ: (stack.length - index - 1) * 4 + randomRotate,
                scale: 1 + index * 0.06 - stack.length * 0.06,
                transformOrigin: '90% 90%'
              }}
              initial={false}
              transition={{
                type: 'spring',
                stiffness: animationConfig.stiffness,
                damping: animationConfig.damping
              }}
            >
              {card.content}
            </motion.div>
          </CardRotate>
        );
      })}
    </div>
  );
}
