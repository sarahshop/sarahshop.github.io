
document.querySelectorAll(".tilt").forEach(card=>{
  card.addEventListener("mousemove",e=>{
    if(window.matchMedia("(pointer:coarse)").matches)return;
    const r=card.getBoundingClientRect();
    const x=(e.clientX-r.left)/r.width-.5;
    const y=(e.clientY-r.top)/r.height-.5;
    card.style.transform=`rotateX(${(-y*7).toFixed(2)}deg) rotateY(${(x*9).toFixed(2)}deg) translateY(-3px)`;
  });
  card.addEventListener("mouseleave",()=>card.style.transform="");
});


// Cursor glow
if(!window.matchMedia("(pointer:coarse)").matches){
  const glow=document.createElement("div");
  glow.className="cursor-glow";
  document.body.appendChild(glow);
  window.addEventListener("mousemove",e=>{
    glow.style.left=e.clientX+"px";
    glow.style.top=e.clientY+"px";
  });
}

// Generic parallax float
document.querySelectorAll(".parallax-float").forEach(el=>{
  window.addEventListener("mousemove",e=>{
    if(window.matchMedia("(pointer:coarse)").matches)return;
    const dx=(e.clientX/window.innerWidth-.5);
    const dy=(e.clientY/window.innerHeight-.5);
    const amt=Number(el.dataset.parallax||18);
    el.style.transform=`translate3d(${dx*amt}px,${dy*amt}px,0)`;
  });
});

// Reveal-on-scroll
const revealObs=new IntersectionObserver(entries=>{
  entries.forEach(en=>{
    if(en.isIntersecting){en.target.classList.add("reveal-in");revealObs.unobserve(en.target)}
  });
},{threshold:.12});
document.querySelectorAll(".reveal").forEach(x=>revealObs.observe(x));
