import{n as e,r as t}from"./rolldown-runtime-hePW80VL.js";import{i as n,t as r}from"./vendor-react-CR5VJ85Q.js";var i=t(n(),1),a=e({default:()=>s}),o=r(),s=({children:e,width:t=200,height:n=80,borderRadius:r=20,borderWidth:a=.07,brightness:s=50,opacity:c=.93,blur:l=11,displace:u=0,backgroundOpacity:d=0,saturation:f=1,distortionScale:p=-180,redOffset:m=0,greenOffset:h=10,blueOffset:g=20,xChannel:_=`R`,yChannel:v=`G`,mixBlendMode:y=`difference`,className:b=``,style:x={}})=>{let S=(0,i.useId)().replace(/:/g,`-`),C=`glass-filter-${S}`,w=`red-grad-${S}`,T=`blue-grad-${S}`,[E,D]=(0,i.useState)(!1),O=(0,i.useRef)(null),k=(0,i.useRef)(null),A=(0,i.useRef)(null),j=(0,i.useRef)(null),M=(0,i.useRef)(null),N=(0,i.useRef)(null),P=()=>{let e=O.current?.getBoundingClientRect(),t=e?.width||400,n=e?.height||200,i=a*.5*Math.min(t,n),o=`
      <svg viewBox="0 0 ${t} ${n}" xmlns="http://www.w3.org/2000/svg">
        <defs>
          <linearGradient id="${w}" x1="100%" y1="0%" x2="0%" y2="0%">
            <stop offset="0%" stop-color="#0000"/>
            <stop offset="100%" stop-color="red"/>
          </linearGradient>
          <linearGradient id="${T}" x1="0%" y1="0%" x2="0%" y2="100%">
            <stop offset="0%" stop-color="#0000"/>
            <stop offset="100%" stop-color="blue"/>
          </linearGradient>
        </defs>
        <rect x="0" y="0" width="${t}" height="${n}" fill="black"></rect>
        <rect x="0" y="0" width="${t}" height="${n}" rx="${r}" fill="url(#${w})" />
        <rect x="0" y="0" width="${t}" height="${n}" rx="${r}" fill="url(#${T})" style="mix-blend-mode: ${y}" />
        <rect x="${i}" y="${i}" width="${t-i*2}" height="${n-i*2}" rx="${r}" fill="hsl(0 0% ${s}% / ${c})" style="filter:blur(${l}px)" />
      </svg>
    `;return`data:image/svg+xml,${encodeURIComponent(o)}`},F=()=>{k.current?.setAttribute(`href`,P())};(0,i.useEffect)(()=>{F(),[{ref:A,offset:m},{ref:j,offset:h},{ref:M,offset:g}].forEach(({ref:e,offset:t})=>{e.current&&(e.current.setAttribute(`scale`,(p+t).toString()),e.current.setAttribute(`xChannelSelector`,_),e.current.setAttribute(`yChannelSelector`,v))}),N.current?.setAttribute(`stdDeviation`,u.toString())},[t,n,r,a,s,c,l,u,p,m,h,g,_,v,y]),(0,i.useEffect)(()=>{if(!O.current)return;let e=new ResizeObserver(()=>{setTimeout(F,0)});return e.observe(O.current),()=>{e.disconnect()}},[]),(0,i.useEffect)(()=>{setTimeout(F,0)},[t,n]),(0,i.useEffect)(()=>{D(I())},[]);let I=()=>{if(typeof window>`u`||typeof document>`u`)return!1;let e=/Safari/.test(navigator.userAgent)&&!/Chrome/.test(navigator.userAgent),t=/Firefox/.test(navigator.userAgent);if(e||t)return!1;let n=document.createElement(`div`);return n.style.backdropFilter=`url(#${C})`,n.style.backdropFilter!==``},L={...x,width:typeof t==`number`?`${t}px`:t,height:typeof n==`number`?`${n}px`:n,borderRadius:`${r}px`,"--glass-frost":d,"--glass-saturation":f,"--filter-id":`url(#${C})`};return(0,o.jsxs)(`div`,{ref:O,className:`glass-surface ${E?`glass-surface--svg`:`glass-surface--fallback`} ${b}`,style:L,children:[(0,o.jsx)(`svg`,{className:`glass-surface__filter`,xmlns:`http://www.w3.org/2000/svg`,children:(0,o.jsx)(`defs`,{children:(0,o.jsxs)(`filter`,{id:C,colorInterpolationFilters:`sRGB`,x:`0%`,y:`0%`,width:`100%`,height:`100%`,children:[(0,o.jsx)(`feImage`,{ref:k,x:`0`,y:`0`,width:`100%`,height:`100%`,preserveAspectRatio:`none`,result:`map`}),(0,o.jsx)(`feDisplacementMap`,{ref:A,in:`SourceGraphic`,in2:`map`,id:`redchannel`,result:`dispRed`}),(0,o.jsx)(`feColorMatrix`,{in:`dispRed`,type:`matrix`,values:`1 0 0 0 0
                      0 0 0 0 0
                      0 0 0 0 0
                      0 0 0 1 0`,result:`red`}),(0,o.jsx)(`feDisplacementMap`,{ref:j,in:`SourceGraphic`,in2:`map`,id:`greenchannel`,result:`dispGreen`}),(0,o.jsx)(`feColorMatrix`,{in:`dispGreen`,type:`matrix`,values:`0 0 0 0 0
                      0 1 0 0 0
                      0 0 0 0 0
                      0 0 0 1 0`,result:`green`}),(0,o.jsx)(`feDisplacementMap`,{ref:M,in:`SourceGraphic`,in2:`map`,id:`bluechannel`,result:`dispBlue`}),(0,o.jsx)(`feColorMatrix`,{in:`dispBlue`,type:`matrix`,values:`0 0 0 0 0
                      0 0 0 0 0
                      0 0 1 0 0
                      0 0 0 1 0`,result:`blue`}),(0,o.jsx)(`feBlend`,{in:`red`,in2:`green`,mode:`screen`,result:`rg`}),(0,o.jsx)(`feBlend`,{in:`rg`,in2:`blue`,mode:`screen`,result:`output`}),(0,o.jsx)(`feGaussianBlur`,{ref:N,in:`output`,stdDeviation:`0.7`})]})})}),(0,o.jsx)(`div`,{className:`glass-surface__content`,children:e})]})};export{a as n,s as t};