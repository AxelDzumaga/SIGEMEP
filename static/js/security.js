document.addEventListener('contextmenu',e=>e.preventDefault());
document.addEventListener('keydown',function(e){let k=(e.key||'').toLowerCase();if(e.key==='F12'||(e.ctrlKey&&['p','s','u'].includes(k))||(e.ctrlKey&&e.shiftKey&&['i','j','c'].includes(k))){e.preventDefault();return false;}});
