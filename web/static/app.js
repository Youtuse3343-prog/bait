(() => {
  'use strict';
  const q=(s,r=document)=>r.querySelector(s), qa=(s,r=document)=>Array.from(r.querySelectorAll(s));

  // Toasts
  qa('.toast').forEach(t=>{q('button',t)?.addEventListener('click',()=>t.remove());setTimeout(()=>{t.classList.add('fade');setTimeout(()=>t.remove(),220)},6500)});

  // Confirmation dialog instead of browser confirm() for destructive actions.
  const dialog=q('#confirm-dialog'), confirmCopy=q('#confirm-copy'), confirmAccept=q('#confirm-accept');
  let pendingForm=null;
  qa('form[data-confirm]').forEach(form=>form.addEventListener('submit',e=>{
    if(form.dataset.confirmed==='1') return;
    e.preventDefault();pendingForm=form;
    if(confirmCopy) confirmCopy.textContent=form.dataset.confirm||'Continue with this action?';
    if(dialog?.showModal) dialog.showModal(); else if(window.confirm(form.dataset.confirm||'Continue?')){form.dataset.confirmed='1';form.submit()}
  }));
  confirmAccept?.addEventListener('click',()=>{if(pendingForm){pendingForm.dataset.confirmed='1';pendingForm.submit();pendingForm=null}});
  qa('form[data-danger-form]').forEach(form=>form.addEventListener('submit',e=>{
    if(form.dataset.confirmed==='1')return;e.preventDefault();pendingForm=form;
    const action=q('[name="moderation_action"], [name="channel_action"]',form)?.value||'action';
    const target=q('[name="moderation_user_id"], [name="moderation_channel_id"]',form)?.value||'selected target';
    const copy=`Apply ${action.replaceAll('_',' ')} to ${target}? Review the target and action before continuing.`;
    if(confirmCopy)confirmCopy.textContent=copy;if(dialog?.showModal)dialog.showModal();else if(window.confirm(copy)){form.dataset.confirmed='1';form.submit()}
  }));

  // Sidebar search + Ctrl/Cmd+K.
  const search=q('#workspace-search');
  const filterNav=()=>{const term=(search?.value||'').trim().toLowerCase();qa('#workspace-nav a').forEach(a=>a.classList.toggle('search-hidden',!!term&&!(`${a.textContent} ${a.dataset.search||''}`.toLowerCase().includes(term))))};
  search?.addEventListener('input',filterNav);
  document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'&&search){e.preventDefault();search.focus();search.select()}});

  // Active section navigation on long pages.
  const nav=q('#workspace-nav');
  if(nav&&'IntersectionObserver'in window){
    const links=qa('a[href^="#"]',nav), targets=links.map(a=>q(a.getAttribute('href'))).filter(Boolean);
    const ob=new IntersectionObserver(entries=>{const v=entries.filter(x=>x.isIntersecting).sort((a,b)=>b.intersectionRatio-a.intersectionRatio)[0];if(!v)return;links.forEach(a=>a.classList.toggle('active',a.getAttribute('href')===`#${v.target.id}`))},{rootMargin:'-18% 0px -68% 0px',threshold:[.03,.18,.45]});
    targets.forEach(t=>ob.observe(t));
  }
  const caseFilter=q('[data-case-filter]');
  caseFilter?.addEventListener('input',()=>{const term=caseFilter.value.trim().toLowerCase();qa('[data-case-row]').forEach(row=>row.hidden=!!term&&!row.textContent.toLowerCase().includes(term))});

  const requested=new URLSearchParams(location.search).get('tab');
  if(requested&&!location.hash){setTimeout(()=>q(`#${CSS.escape(requested)}`)?.scrollIntoView({behavior:'smooth',block:'start'}),80)}

  // Dirty-form tracking and leave protection.
  let dirtyCount=0;
  qa('form[data-dirty-form]').forEach(form=>{
    let dirty=false;const bar=form.querySelector('.savebar')||q('.savebar');const label=form.querySelector('[data-dirty-label]')||q('[data-dirty-label]',bar||document);
    const mark=()=>{if(!dirty){dirty=true;dirtyCount++}bar?.classList.add('dirty');if(label)label.textContent='Unsaved changes'};
    form.addEventListener('input',mark);form.addEventListener('change',mark);
    form.addEventListener('submit',()=>{if(dirty){dirty=false;dirtyCount=Math.max(0,dirtyCount-1)}});
    form.addEventListener('reset',()=>setTimeout(()=>{if(dirty){dirty=false;dirtyCount=Math.max(0,dirtyCount-1)}bar?.classList.remove('dirty');if(label)label.textContent='All changes saved'},0));
  });
  window.addEventListener('beforeunload',e=>{if(dirtyCount>0){e.preventDefault();e.returnValue=''}});

  // Live server snapshot stream.
  const live=q('[data-live-endpoint]');
  if(live&&window.EventSource){
    const es=new EventSource(live.dataset.liveEndpoint);
    es.onmessage=e=>{try{const d=JSON.parse(e.data);const set=(name,val)=>{const el=q(`[data-live="${name}"]`);if(el&&val!==undefined&&val!==null)el.textContent=val};set('bot',d.bot_online?'Online':'Offline');set('latency',d.latency_ms==null?'Worker/snapshot mode':`${d.latency_ms} ms latency`);set('members',Number(d.member_count||0).toLocaleString());set('online',Number(d.online||0).toLocaleString());set('tickets',d.open_tickets);set('cases',d.cases_today);set('event',(d.last_event||'No recent activity').replaceAll('_',' ').replaceAll('.',' · '));set('updated',`Updated ${new Date(d.ts).toLocaleTimeString()}`)}catch(_){}};
    window.addEventListener('beforeunload',()=>es.close());
  }

  // Server Stats preview, role-aware values, and drag ordering.
  const stats=q('#server-stats'), statsBuilder=q('#stats-builder');
  const wireStatRow=row=>{
    const type=q('[data-stat-type]',row),role=q('.stat-role-select',row),preview=q('[data-stat-preview]',row),emoji=q('input[name^="stat_emoji_"]',row),label=q('input[name^="stat_label_"]',row),template=q('input[name^="stat_template_"]',row);
    const count=()=>{if(type?.value==='role')return role?.selectedOptions[0]?.dataset.count||'0';const map={members:stats?.dataset.membersCount,humans:stats?.dataset.humansCount,bots:stats?.dataset.botsCount,boosts:stats?.dataset.boostsCount};return map[type?.value]??(type?.value==='open_tickets'?q('[data-live="tickets"]')?.textContent:'0')};
    const refresh=()=>{if(!preview)return;let out=(template?.value||'{emoji} {label}: {value}').replaceAll('{emoji}',emoji?.value||'').replaceAll('{label}',label?.value||'Stat').replaceAll('{value}',count());preview.textContent=out.replace(/\s+/g,' ').trim()||'Preview'};
    [type,role,emoji,label,template].filter(Boolean).forEach(x=>{x.addEventListener('input',refresh);x.addEventListener('change',refresh)});refresh();
  };
  qa('[data-stat-row]').forEach(wireStatRow);
  if(statsBuilder){
    let dragged=null;qa('[data-stat-row]',statsBuilder).forEach(row=>{row.addEventListener('dragstart',()=>{dragged=row;row.classList.add('dragging')});row.addEventListener('dragend',()=>{row.classList.remove('dragging');dragged=null});row.addEventListener('dragover',e=>{e.preventDefault();if(!dragged||dragged===row)return;const rect=row.getBoundingClientRect();statsBuilder.insertBefore(dragged,e.clientY<rect.top+rect.height/2?row:row.nextSibling)})});
    q('#config-form')?.addEventListener('submit',()=>{qa('[data-stat-row]',statsBuilder).forEach((row,i)=>qa('[name]',row).forEach(el=>{el.name=el.name.replace(/_(\d+)$/,`_${i}`)}))});
  }

  // Color field labels in appearance.
  qa('.color-field input[type="color"]').forEach(input=>input.addEventListener('input',()=>{const code=q('code',input.closest('.color-field'));if(code)code.textContent=input.value}));

  // Message Studio.
  const form=q('#message-builder-form');
  if(form){
    const by=n=>q(`[name="${n}"]`,form), val=n=>(by(n)?.value||'').trim(), checked=n=>!!by(n)?.checked;
    const prev={content:q('[data-preview-content]'),embed:q('[data-preview-embed]'),author:q('[data-preview-author]'),title:q('[data-preview-title]'),description:q('[data-preview-description]'),fields:q('[data-preview-fields]'),thumb:q('[data-preview-thumbnail]'),image:q('[data-preview-image]'),footer:q('[data-preview-footer]'),buttons:q('[data-preview-buttons]')};
    const text=(el,v,f='')=>{if(!el)return;const s=(v||'').trim();el.textContent=s||f;el.hidden=!s&&!f};
    const image=(el,u)=>{if(!el)return;if(/^https?:\/\//i.test(u||'')){el.src=u;el.hidden=false}else{el.removeAttribute('src');el.hidden=true}};
    const updateFields=()=>{if(!prev.fields)return;prev.fields.replaceChildren();qa('[data-embed-field-row]',form).forEach(row=>{const n=(q('[data-field-name]',row)?.value||'').trim(),v=(q('[data-field-value]',row)?.value||'').trim();if(!n||!v)return;const d=document.createElement('div');d.className=`preview-field${q('[data-field-inline]',row)?.checked?' inline':''}`;const s=document.createElement('strong'),t=document.createElement('div');s.textContent=n;t.textContent=v;d.append(s,t);prev.fields.append(d)});prev.fields.hidden=!prev.fields.children.length};
    const updateButtons=()=>{if(!prev.buttons)return;prev.buttons.replaceChildren();qa('.component-row',form).forEach(row=>{if(!q('input[type="checkbox"]',row)?.checked)return;const label=q('input[name^="button_label_"]',row)?.value.trim();if(!label)return;const span=document.createElement('span');const kind=q('[data-button-kind]',row)?.value||'link';span.className=kind;span.textContent=`${q('input[name^="button_emoji_"]',row)?.value||''} ${label}`.trim();prev.buttons.append(span)})};
    const update=()=>{text(prev.content,val('action_content'));if(prev.embed)prev.embed.hidden=!checked('action_use_embed');if(checked('action_use_embed')){text(prev.author,val('embed_author_name'));text(prev.title,val('embed_title'),'Announcement title');text(prev.description,val('embed_description'),'Write your embed and the preview updates as you type.');image(prev.thumb,val('embed_thumbnail_url'));image(prev.image,val('embed_image_url'));text(prev.footer,[val('embed_footer_text'),checked('embed_timestamp')?'Today at 12:00 PM':''].filter(Boolean).join(' • '));updateFields()}updateButtons()};
    const picker=q('[data-color-picker]',form),colorText=q('[data-color-text]',form);const norm=r=>{let h=(r||'').trim();if(!h.startsWith('#'))h='#'+h;if(/^#[0-9a-f]{6}$/i.test(h))return h.toLowerCase();if(/^#[0-9a-f]{3}$/i.test(h))return '#'+h.slice(1).split('').map(c=>c+c).join('').toLowerCase();return null};const applyColor=r=>{const h=norm(r)||'#7c3aed';if(prev.embed)prev.embed.style.borderLeftColor=h;return h};picker?.addEventListener('input',()=>{if(colorText)colorText.value=picker.value;applyColor(picker.value)});colorText?.addEventListener('input',()=>{const h=norm(colorText.value);if(h&&picker)picker.value=h;if(h)applyColor(h)});
    q('#add-embed-field')?.addEventListener('click',e=>{const next=qa('[data-embed-field-row].is-hidden',form)[0];if(next){next.classList.remove('is-hidden');q('input,textarea',next)?.focus()}if(!qa('[data-embed-field-row].is-hidden',form).length)e.currentTarget.disabled=true});
    qa('[data-button-kind]',form).forEach(sel=>{const refresh=()=>sel.closest('.component-row')?.classList.toggle('role-mode',sel.value==='role');sel.addEventListener('change',refresh);refresh()});
    form.addEventListener('input',update);form.addEventListener('change',update);applyColor(colorText?.value);update();

    // Scheduling stores the browser's local selection as an ISO UTC instant.
    q('[data-schedule-submit]',form)?.addEventListener('click',e=>{const local=q('#schedule-local'),hidden=q('#schedule-at-iso');if(local?.value&&hidden){const d=new Date(local.value);if(!Number.isNaN(d.getTime()))hidden.value=d.toISOString()}});

    // Saved template loader.
    const set=(n,v)=>{const el=by(n);if(!el)return;if(el.type==='checkbox')el.checked=!!v;else el.value=v??''};
    qa('[data-load-template]').forEach(btn=>btn.addEventListener('click',async()=>{btn.disabled=true;try{const r=await fetch(btn.dataset.loadTemplate,{headers:{Accept:'application/json'}});if(!r.ok)throw new Error();const row=await r.json(),p=row.payload||{};set('template_name',row.name||'');set('action_content',p.content||'');set('action_allow_mentions',p.allow_mentions);set('action_publish',p.publish);const e=p.embed||{};set('action_use_embed',!!p.embed);['title','title_url','description','author_name','author_url','author_icon_url','thumbnail_url','image_url','footer_text','footer_icon_url'].forEach(k=>set(`embed_${k}`,e[k]||''));if(e.color!=null){const hex='#'+Number(e.color).toString(16).padStart(6,'0');set('embed_color',hex);if(picker)picker.value=hex;applyColor(hex)}set('embed_timestamp',e.timestamp);qa('[data-embed-field-row]',form).forEach((row,i)=>{const f=(e.fields||[])[i]||{};q('[data-field-name]',row).value=f.name||'';q('[data-field-value]',row).value=f.value||'';q('[data-field-inline]',row).checked=!!f.inline;row.classList.toggle('is-hidden',i>=Math.max(3,(e.fields||[]).length))});qa('.component-row',form).forEach((row,i)=>{const b=(p.buttons||[])[i]||{};q('input[type="checkbox"]',row).checked=!!b.label;q('input[name^="button_label_"]',row).value=b.label||'';q('input[name^="button_emoji_"]',row).value=b.emoji||'';q('[data-button-kind]',row).value=b.kind||'link';q('[data-button-url]',row).value=b.url||'';q('[data-button-role]',row).value=b.role_id||'';q('[data-button-mode]',row).value=b.mode||'toggle';row.classList.toggle('role-mode',b.kind==='role')});update();form.scrollIntoView({behavior:'smooth',block:'start'})}catch(_){alert('Could not load that template.')}finally{btn.disabled=false}}));
  }
})();
