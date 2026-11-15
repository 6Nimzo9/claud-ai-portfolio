const $=id=>document.getElementById(id);
let currentId=null,busy=false;
const ACTIVE_CONVERSATION_KEY="claud_ai_active_conversation_v1";
const CONVERSATION_TITLES_KEY="claud_ai_conversation_titles_v1";
function readTitleMap(){try{return JSON.parse(localStorage.getItem(CONVERSATION_TITLES_KEY)||"{}")}catch{return {}}}
function writeTitle(id,title){const map=readTitleMap();map[String(id)]=title;localStorage.setItem(CONVERSATION_TITLES_KEY,JSON.stringify(map))}
function makeTitle(text){const clean=String(text||"").replace(/\s+/g," ").trim();if(!clean)return "New chat";const sentence=clean.split(/(?<=[.!?])\s+/)[0]||clean;return sentence.length>44?sentence.slice(0,41).trimEnd()+"…":sentence}
function isGenericTitle(title){return !title||/^(conversation|claud ai conversation|new conversation|new chat)$/i.test(title.trim())}
async function api(path,opts={}){const r=await fetch(path,{headers:{"Content-Type":"application/json"},...opts});const d=await r.json();if(!r.ok)throw Error(d.error||"Request failed");return d}
function esc(s){return String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]))}
function renderAssistantMarkdown(text){
 const raw=window.marked.parse(String(text??""),{gfm:true,breaks:false});
 const safe=window.DOMPurify.sanitize(raw);
 const fragment=document.createElement("div");
 fragment.innerHTML=safe;

 fragment.querySelectorAll("pre code").forEach(code=>{
  if(window.hljs){
   try{window.hljs.highlightElement(code)}catch(error){console.warn("Highlight error:",error)}
  }

  const pre=code.closest("pre");
  if(!pre)return;

  const copyButton=document.createElement("button");
  copyButton.type="button";
  copyButton.className="code-copy-btn";
  copyButton.textContent="Copy";
  copyButton.setAttribute("aria-label","Copy code");

  copyButton.addEventListener("click",async()=>{
   try{
    await navigator.clipboard.writeText(code.textContent);
    copyButton.textContent="Copied!";
    setTimeout(()=>{copyButton.textContent="Copy"},1500);
   }catch(error){
    console.error("Clipboard error:",error);
    copyButton.textContent="Failed";
    setTimeout(()=>{copyButton.textContent="Copy"},1500);
   }
  });

  pre.appendChild(copyButton);
 });

 return fragment.innerHTML;
}

function feedbackStoreKey(text){return `claud_ai_feedback_${currentId||"none"}_${String(text||"").length}_${String(text||"").slice(0,48)}`}
function buildMessageActions(div,text){
 const actions=document.createElement("div");
 actions.className="message-actions";
 actions.innerHTML='<button data-action="copy" title="Copy response" aria-label="Copy response">▢ <span>Copy</span></button><button data-action="regenerate" title="Regenerate response" aria-label="Regenerate response">↻ <span>Regenerate</span></button><button data-action="good" title="Good response" aria-label="Good response">👍</button><button data-action="bad" title="Bad response" aria-label="Bad response">👎</button><button data-action="more" title="More actions" aria-label="More actions">⋯</button>';
 const copy=actions.querySelector('[data-action="copy"]');
 copy.onclick=async()=>{try{await navigator.clipboard.writeText(text);const label=copy.querySelector("span");label.textContent="Copied";setTimeout(()=>label.textContent="Copy",1300)}catch{alert("Clipboard access is unavailable in this browser.")}};
 actions.querySelector('[data-action="regenerate"]').onclick=()=>regenerate(div);
 const key=feedbackStoreKey(text);
 let saved="";try{saved=localStorage.getItem(key)||""}catch{}
 for(const [kind,value] of [["good","up"],["bad","down"]]){
  const button=actions.querySelector(`[data-action="${kind}"]`);
  if(saved===value)button.classList.add("selected");
  button.onclick=()=>{
   const next=button.classList.contains("selected")?"":value;
   actions.querySelectorAll('[data-action="good"],[data-action="bad"]').forEach(b=>b.classList.remove("selected"));
   if(next)button.classList.add("selected");
   try{if(next)localStorage.setItem(key,next);else localStorage.removeItem(key)}catch{}
   button.title=next?(next==="up"?"Positive feedback saved on this device":"Negative feedback saved on this device"):"Feedback cleared";
  };
 }
 const more=actions.querySelector('[data-action="more"]');
 more.onclick=(event)=>{
  event.stopPropagation();
  document.querySelectorAll(".message-more-menu").forEach(menu=>menu.remove());
  const menu=document.createElement("div");
  menu.className="message-more-menu";
  menu.innerHTML='<button data-cmd="copy">Copy response</button><button data-cmd="regenerate">Regenerate response</button>';
  actions.appendChild(menu);
  menu.querySelector('[data-cmd="copy"]').onclick=()=>{copy.click();menu.remove()};
  menu.querySelector('[data-cmd="regenerate"]').onclick=()=>{menu.remove();regenerate(div)};
  const close=e=>{if(!menu.contains(e.target)&&e.target!==more){menu.remove();document.removeEventListener("click",close)}};
  setTimeout(()=>document.addEventListener("click",close),0);
 };
 div.querySelector(".msg-content").appendChild(actions);
}
function addMessage(role,text,status=""){
 const box=$("messages"),div=document.createElement("div");
 div.className="msg "+role;
 const rendered=role==="assistant"?renderAssistantMarkdown(text):esc(text);
 div.innerHTML=`<div class="who">${role==="user"?"C":"✳"}</div><div class="msg-content"><div class="bubble">${rendered}</div>${status?`<div class="meta">${esc(status)}</div>`:""}</div>`;
 if(role==="assistant")buildMessageActions(div,text);
 box.appendChild(div);box.scrollTop=box.scrollHeight;updateSessionPanel();return div;
}
async function regenerate(messageNode){
 if(busy||!currentId)return;
 setBusy(true);
 const placeholder=document.createElement("div");
 placeholder.className="msg assistant";
 placeholder.innerHTML='<div class="who">✳</div><div class="msg-content"><div class="bubble"><span class="thinking-label">Regenerating response</span><span class="thinking-dots"><i></i><i></i><i></i></span></div></div>';
 messageNode.after(placeholder);
 try{
  const d=await api(`/api/conversations/${currentId}/regenerate`,{method:"POST",body:"{}"});
  const replacement=document.createElement("div");
  replacement.className="msg assistant";
  replacement.innerHTML=`<div class="who">✳</div><div class="msg-content"><div class="bubble">${renderAssistantMarkdown(d.answer)}</div><div class="meta">${esc(d.status)} · ${Number(d.duration_seconds||0).toFixed(1)}s</div></div>`;
  buildMessageActions(replacement,d.answer);
  messageNode.replaceWith(replacement);
  await refreshConversations();
 }catch(e){alert("Regenerate failed: "+e.message)}
 finally{placeholder.remove();setBusy(false)}
}

function setBusy(v){busy=v;$("sendBtn").disabled=v;$("sendBtn").textContent=v?"…":"↑";if($("intelActivity"))$("intelActivity").textContent=v?"Generating a response…":"Ready · Awaiting your instruction"}
async function refreshConversations(){
 const d=await api("/api/conversations"),titleMap=readTitleMap();
 const rows=await Promise.all((d.items||[]).map(async x=>{let title=titleMap[String(x.id)]||x.title;if(isGenericTitle(title)){try{const md=await api(`/api/conversations/${x.id}/messages`);const first=(md.items||[]).find(m=>m.role==="user"&&m.content);if(first){title=makeTitle(first.content);writeTitle(x.id,title)}}catch{}}return {...x,displayTitle:isGenericTitle(title)?"New chat":title}}));
 $("conversationList").innerHTML=rows.map(x=>`<div class="conv-row ${Number(x.id)===Number(currentId)?"active":""}"><button class="conv" data-id="${x.id}" title="${esc(x.displayTitle)}"><span class="conv-title">${esc(x.displayTitle)}</span></button><button class="conv-menu-btn" data-menu-id="${x.id}" aria-label="Conversation options">⋯</button></div>`).join("");
 document.querySelectorAll(".conv[data-id]").forEach(b=>b.onclick=()=>openConversation(Number(b.dataset.id)));
 document.querySelectorAll(".conv-menu-btn").forEach(b=>b.onclick=e=>{e.stopPropagation();showConversationMenu(Number(b.dataset.menuId),b)});
 updateSessionPanel();
}
function showConversationMenu(id,anchor){
 document.querySelectorAll(".conversation-popover").forEach(x=>x.remove());const menu=document.createElement("div");menu.className="conversation-popover";menu.innerHTML='<button data-cmd="rename">✎ Rename</button><button data-cmd="delete" class="danger">⌫ Delete conversation</button>';
 anchor.parentElement.appendChild(menu);
 menu.querySelector('[data-cmd="rename"]').onclick=async()=>{menu.remove();const current=anchor.parentElement.querySelector(".conv-title").textContent;const title=prompt("Rename conversation",current);if(title===null||!title.trim())return;try{await api(`/api/conversations/${id}`,{method:"PATCH",body:JSON.stringify({title:title.trim()})});const map=readTitleMap();map[String(id)]=title.trim();localStorage.setItem(CONVERSATION_TITLES_KEY,JSON.stringify(map));await refreshConversations()}catch(e){alert(e.message)}};
 menu.querySelector('[data-cmd="delete"]').onclick=async()=>{menu.remove();if(!confirm("Delete this conversation and its messages? This cannot be undone. Memories and Knowledge will not be deleted."))return;try{await api(`/api/conversations/${id}`,{method:"DELETE"});const map=readTitleMap();delete map[String(id)];localStorage.setItem(CONVERSATION_TITLES_KEY,JSON.stringify(map));if(Number(currentId)===id){currentId=null;sessionStorage.removeItem(ACTIVE_CONVERSATION_KEY);$("messages").innerHTML="";$("welcome").classList.remove("hidden");await createConversation()}else await refreshConversations()}catch(e){alert("Delete failed: "+e.message)}};
 document.addEventListener("click",function close(ev){if(!menu.contains(ev.target)&&ev.target!==anchor){menu.remove();document.removeEventListener("click",close)}},{once:true});
}

async function openConversation(id){
 currentId=id;sessionStorage.setItem(ACTIVE_CONVERSATION_KEY,String(id));
 $("messages").innerHTML="";$("welcome").classList.add("hidden");
 const d=await api(`/api/conversations/${id}/messages`);d.items.forEach(m=>addMessage(m.role,m.content));
 updateSessionPanel();await refreshConversations();
}
async function createConversation(){
 const d=await api("/api/conversations",{method:"POST",body:"{}"});
 currentId=d.id;sessionStorage.setItem(ACTIVE_CONVERSATION_KEY,String(d.id));
 $("messages").innerHTML="";$("welcome").classList.remove("hidden");
 await refreshConversations();
}
async function send(){if(busy)return;const input=$("messageInput"),text=input.value.trim();if(!text)return;if(!currentId)await createConversation();
 const titleMap=readTitleMap();
 if(!titleMap[String(currentId)])writeTitle(currentId,makeTitle(text));
 input.value="";input.style.height="auto";$("welcome").classList.add("hidden");addMessage("user",text);setBusy(true);const placeholder=document.createElement("div");placeholder.className="msg assistant";placeholder.id="thinking";placeholder.innerHTML='<div class="who">✳</div><div class="bubble"><span class="thinking-label">Claud is thinking</span><span class="thinking-dots" aria-label="Thinking"><i></i><i></i><i></i></span></div>';$("messages").appendChild(placeholder);$("messages").scrollTop=$("messages").scrollHeight;try{const d=await api("/api/chat",{method:"POST",body:JSON.stringify({conversation_id:currentId,message:text})});placeholder.remove();addMessage("assistant",d.answer,`${d.status} · ${Number(d.duration_seconds||0).toFixed(1)}s`);await refreshConversations()}catch(e){placeholder.remove();addMessage("assistant","Terjadi kesalahan: "+e.message)}finally{setBusy(false)}}
$("sendBtn").onclick=send;
$("attachBtn").onclick=()=>$ ("fileInput").click();
$("fileInput").onchange=async e=>{const file=e.target.files?.[0];if(!file)return;try{if(file.size>150000)throw Error("File must be 300 KB or smaller.");const content=await file.text();$("messageInput").value+=`\n\n[Attached file: ${file.name}]\n${content}`;$("messageInput").focus()}catch(err){alert(err.message)}finally{e.target.value=""}};
$("profileTrigger").onclick=()=>{const dd=$("profileDropdown");dd.classList.toggle("hidden");$("profileTrigger").setAttribute("aria-expanded",String(!dd.classList.contains("hidden")))};
$("messageInput").addEventListener("keydown",e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();send()}});$("messageInput").addEventListener("input",e=>{e.target.style.height="auto";e.target.style.height=Math.min(e.target.scrollHeight,180)+"px"});$("newChat").onclick=createConversation;$("menu").onclick=()=>$("sidebar").classList.toggle("open");
document.querySelectorAll("[data-prompt]").forEach(b=>b.onclick=()=>{$("messageInput").value=b.dataset.prompt;send()});
document.addEventListener("keydown",e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==="k"){e.preventDefault();createConversation();$("messageInput").focus()}});
function updateSessionPanel(){const count=$("messages").querySelectorAll(".msg").length;$("intelMessages").textContent=String(count);$("intelConversation").textContent=currentId?`#${currentId}`:"New chat";}
async function refreshIntelligence(){try{const d=await api("/api/health");$("intelState").textContent=d.ok?"Systems operational":"Degraded state";$("intelModel").textContent=`Local model · ${d.model||"unknown"}`;$("intelCore").textContent=d.core||"Unknown";$("intelOllama").textContent=d.ollama?"Connected":"Offline";$("intelModelName").textContent=d.model||"—";$("intelReasoning").textContent=d.reasoning?"Enabled":"Disabled";$("intelCritique").textContent=d.critique?"Enabled":"Disabled";$("intelGrounding").textContent=d.grounding?"Enabled":"Disabled";$("intelActivity").textContent=busy?"Generating a response…":"Ready · Awaiting your instruction";}catch{$("intelState").textContent="Connection unavailable";$ ("intelActivity").textContent="Unable to read runtime status"}}
setInterval(refreshIntelligence,10000);
function show(view) {
    ["chat", "memory", "knowledge", "system", "development"].forEach(v => {
        $(v + "View").classList.toggle("hidden", v !== view);
    });

    $("pageTitle").textContent = {
        chat: "Claud AI",
        memory: "Memory",
        knowledge: "Knowledge base",
        system: "System status",
        development: "Development Engine"
    }[view];

    $("pageSub").textContent = {
        chat: "Your thinking partner",
        memory: "Persistent personal context",
        knowledge: "Structured knowledge",
        system: "Local runtime diagnostics",
        development: "Task planning and approval"
    }[view];

    document.querySelectorAll(".nav").forEach(button => {
        button.classList.toggle("active", button.dataset.view === view);
    });

    if (view === "memory") loadMemory();
    if (view === "knowledge") loadKnowledge();
    if (view === "system") loadSystem();
    if (view === "development") loadDevelopmentTasks();
}
document.querySelectorAll(".nav").forEach(button => {button.onclick=()=>{show(button.dataset.view);$("profileDropdown").classList.add("hidden");$("profileTrigger").setAttribute("aria-expanded","false")}});
async function loadMemory(){const d=await api("/api/memories");$("memoryList").innerHTML=d.items.map(x=>`<div class="item"><small>Memory #${x.id}</small><div>${esc(x.content)}</div></div>`).join("")||"<p>No memories saved.</p>"}
$("saveMemory").onclick=async()=>{const input=$("memoryInput");if(!input.value.trim())return;await api("/api/memories",{method:"POST",body:JSON.stringify({content:input.value})});input.value="";loadMemory()};
async function loadKnowledge(){const d=await api("/api/knowledge");$("knowledgeList").innerHTML=d.items.map(x=>`<div class="item"><small>${esc(x.category)} · ${esc(x.status)} · #${x.id}</small><div>${esc(x.content)}</div></div>`).join("")||"<p>No knowledge entries.</p>"}
async function loadSystem(){const d=await api("/api/health");$("modelName").textContent=d.model;$("ollamaDot").style.background=d.ollama?"#42a879":"#d65d5d";$("statusDot").style.background=d.ok?"#42a879":"#d65d5d";$("systemCards").innerHTML=[["Core",d.core],["Ollama",d.ollama?"Connected":"Offline"],["Model",d.model],["Version",d.version],["Reasoning",d.reasoning?"Enabled":"Disabled"],["Self-critique",d.critique?"Enabled":"Disabled"],["Grounding",d.grounding?"Enabled":"Disabled"]].map(x=>`<div class="status-card"><small>${x[0]}</small><b>${esc(x[1])}</b></div>`).join("");$("toggles").innerHTML=[["reasoning_enabled","Reasoning",d.reasoning],["critique_enabled","Self-critique",d.critique],["grounding_enabled","Grounding",d.grounding]].map(x=>`<label class="toggle">${x[1]}<input type="checkbox" data-key="${x[0]}" ${x[2]?"checked":""}></label>`).join("");document.querySelectorAll("[data-key]").forEach(el=>el.onchange=async()=>{await api("/api/settings/toggle",{method:"POST",body:JSON.stringify({key:el.dataset.key,value:el.checked})});loadSystem()})}
$("refreshStatus").onclick=loadSystem;
(async()=>{
 try{
  await loadSystem();
  const d=await api("/api/conversations");
  const savedId=sessionStorage.getItem(ACTIVE_CONVERSATION_KEY);
  const savedExists=savedId&&(d.items||[]).some(x=>String(x.id)===String(savedId));
  if(savedExists)await openConversation(Number(savedId));
  else await createConversation();
 }catch(e){$("modelName").textContent="Backend unavailable";addMessage("assistant","Tidak dapat menghubungi backend Claud AI: "+e.message)}
})();
// DEVELOPMENT_ENGINE_FRONTEND_V1
let selectedDevelopmentTaskId = null;

function developmentDate(value) {
    if (!value) return "-";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? esc(value) : date.toLocaleString();
}

function developmentStatus(status) {
    return String(status || "unknown").replaceAll("_", " ");
}

async function loadDevelopmentTasks() {
    const list = $("developmentTasks");
    list.textContent = "Loading tasks...";

    try {
        const data = await api("/api/development/tasks");
        const items = data.items || [];

        list.innerHTML = items.length
            ? items.map(task => `
                <button class="development-task ${task.task_id === selectedDevelopmentTaskId ? "selected" : ""}"
                        data-development-id="${esc(task.task_id)}">
                    <strong>${esc(task.title)}</strong>
                    <small>${esc(developmentStatus(task.status))}</small>
                    <small>${esc(developmentDate(task.created_at))}</small>
                </button>
            `).join("")
            : '<p class="development-empty">No development tasks yet.</p>';

        $("developmentTasks").querySelectorAll("[data-development-id]").forEach(button => {
            button.onclick = () => openDevelopmentTask(button.dataset.developmentId);
        });

        if (data.execution_enabled === true) {
            $("developmentMessage").textContent =
                "WARNING: Backend reports execution_enabled=true. Do not approve tasks.";
        }

        if (selectedDevelopmentTaskId &&
            items.some(task => task.task_id === selectedDevelopmentTaskId)) {
            await openDevelopmentTask(selectedDevelopmentTaskId);
        } else if (items.length) {
            await openDevelopmentTask(items[0].task_id);
        } else {
            selectedDevelopmentTaskId = null;
            $("developmentDetail").textContent = "No task selected.";
        }
    } catch (error) {
        list.innerHTML = `<p class="development-error">${esc(error.message)}</p>`;
    }
}

async function openDevelopmentTask(taskId) {
    selectedDevelopmentTaskId = taskId;
    const detail = $("developmentDetail");
    detail.textContent = "Loading task details...";

    try {
        const report = await api(`/api/development/tasks/${encodeURIComponent(taskId)}`);
        const task = report.task || {};
        const plan = report.plan || {};
        const steps = plan.steps || [];
        const events = report.events || [];

        const actions = task.status === "awaiting_approval"
            ? `<div class="development-actions">
                 <button class="development-approve" id="approveDevelopmentTask">Approve</button>
                 <button class="development-reject" id="rejectDevelopmentTask">Reject</button>
               </div>`
            : "";

        detail.innerHTML = `
            <div class="development-detail-heading">
                <h3>${esc(task.title)}</h3>
                <span class="development-status">${esc(developmentStatus(task.status))}</span>
            </div>

            <div class="development-meta">
                <small>Created: ${esc(developmentDate(task.created_at))}</small>
                <small>Task ID: ${esc(task.task_id)}</small>
                <small>Plan version: ${esc(plan.version || "-")}</small>
            </div>

            <h4>Objective</h4>
            <p class="development-objective">${esc(task.objective)}</p>

            <h4>Development plan</h4>
            <div class="development-steps">
                ${steps.map(step => `
                    <div class="development-step">
                        <span class="step-number">${esc(step.sequence)}</span>
                        <div>
                            <strong>${esc(step.name)}</strong>
                            <p>${esc(step.description)}</p>
                            <small>${esc(developmentStatus(step.status))}</small>
                        </div>
                    </div>
                `).join("")}
            </div>

            ${actions}

            <h4>Event history</h4>
            <div class="development-events">
                ${events.length ? events.map(event => `
                    <div class="development-event">
                        <strong>${esc(event.event_type)}</strong>
                        <small>${esc(developmentDate(event.created_at))}</small>
                        <p>${esc(event.from_status || "-")} → ${esc(event.to_status || "-")}</p>
                        <small>${esc(JSON.stringify(event.details || {}))}</small>
                    </div>
                `).join("") : "<p>No events recorded.</p>"}
            </div>

            <p class="development-execution-note">
                Execution enabled: <strong>${report.execution_enabled === true ? "YES — WARNING" : "NO"}</strong>
            </p>
        `;

        $("developmentTasks").querySelectorAll("[data-development-id]").forEach(button => {
            button.classList.toggle("selected", button.dataset.developmentId === taskId);
        });

        const approveButton = $("approveDevelopmentTask");
        const rejectButton = $("rejectDevelopmentTask");

        if (approveButton) {
            approveButton.onclick = () => changeDevelopmentTaskStatus(taskId, "approve");
        }

        if (rejectButton) {
            rejectButton.onclick = () => changeDevelopmentTaskStatus(taskId, "reject");
        }
    } catch (error) {
        detail.innerHTML = `<p class="development-error">${esc(error.message)}</p>`;
    }
}

async function createDevelopmentTask() {
    const title = $("developmentTaskTitle").value.trim();
    const objective = $("developmentTaskDescription").value.trim();
    const message = $("developmentMessage");

    if (!title || !objective) {
        message.textContent = "Title and objective are both required.";
        return;
    }

    const button = $("createDevelopmentTask");
    button.disabled = true;
    message.textContent = "Creating task...";

    try {
        const report = await api("/api/development/tasks", {
            method: "POST",
            body: JSON.stringify({ title, objective })
        });

        $("developmentTaskTitle").value = "";
        $("developmentTaskDescription").value = "";
        selectedDevelopmentTaskId = report.task.task_id;
        message.textContent = "Task created. Awaiting your review.";
        await loadDevelopmentTasks();
    } catch (error) {
        message.textContent = "Failed to create task: " + error.message;
    } finally {
        button.disabled = false;
    }
}

async function changeDevelopmentTaskStatus(taskId, action) {
    const label = action === "approve" ? "approve" : "reject";

    if (!window.confirm(`Are you sure you want to ${label} this task?`)) {
        return;
    }

    const approveButton = $("approveDevelopmentTask");
    const rejectButton = $("rejectDevelopmentTask");

    if (approveButton) approveButton.disabled = true;
    if (rejectButton) rejectButton.disabled = true;

    try {
        await api(`/api/development/tasks/${encodeURIComponent(taskId)}/${action}`, {
            method: "POST",
            body: "{}"
        });

        $("developmentMessage").textContent =
            action === "approve"
                ? "Task approved. No code was executed."
                : "Task rejected.";

        await loadDevelopmentTasks();
    } catch (error) {
        $("developmentMessage").textContent =
            "Action failed: " + error.message;
        await openDevelopmentTask(taskId);
    }
}

$("developmentRefresh").onclick = loadDevelopmentTasks;
$("createDevelopmentTask").onclick = createDevelopmentTask;

// END DEVELOPMENT_ENGINE_FRONTEND_V1

/* =========================================================
   CLAUD AI — AMBIENT PARTICLE FIELD
   Lightweight canvas animation with reduced-motion support.
   ========================================================= */
(function initClaudMotion() {
  const main = document.querySelector(".main");
  if (!main || document.getElementById("claudParticles")) return;

  const reduceMotion = window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduceMotion) return;

  const canvas = document.createElement("canvas");
  canvas.id = "claudParticles";
  canvas.setAttribute("aria-hidden", "true");
  main.prepend(canvas);

  const ctx = canvas.getContext("2d", { alpha: true });
  if (!ctx) return;

  let width = 0, height = 0, raf = 0, lastFrame = 0;
  let particles = [];
  const dpr = Math.min(window.devicePixelRatio || 1, 1.5);

  function resize() {
    const rect = main.getBoundingClientRect();
    width = Math.max(1, rect.width);
    height = Math.max(1, rect.height);
    canvas.width = Math.floor(width * dpr);
    canvas.height = Math.floor(height * dpr);
    canvas.style.width = width + "px";
    canvas.style.height = height + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    const count = Math.max(22, Math.min(58, Math.floor((width * height) / 22000)));
    particles = Array.from({ length: count }, () => ({
      x: Math.random() * width,
      y: Math.random() * height,
      vx: (Math.random() - .5) * .22,
      vy: (Math.random() - .5) * .22,
      r: Math.random() * 1.5 + .45,
      phase: Math.random() * Math.PI * 2
    }));
  }

  function draw(time) {
    if (document.hidden) {
      raf = 0;
      return;
    }
    if (time - lastFrame < 32) {
      raf = requestAnimationFrame(draw);
      return;
    }
    lastFrame = time;
    ctx.clearRect(0, 0, width, height);

    const linkDistance = 115;
    for (let i = 0; i < particles.length; i++) {
      const p = particles[i];
      p.x += p.vx;
      p.y += p.vy;
      if (p.x < -8) p.x = width + 8;
      if (p.x > width + 8) p.x = -8;
      if (p.y < -8) p.y = height + 8;
      if (p.y > height + 8) p.y = -8;

      const pulse = .45 + (Math.sin(time * .001 + p.phase) + 1) * .22;
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.r * pulse, 0, Math.PI * 2);
      ctx.fillStyle = "rgba(102,228,255," + (.28 + pulse * .35) + ")";
      ctx.fill();

      for (let j = i + 1; j < particles.length; j++) {
        const q = particles[j];
        const dx = p.x - q.x, dy = p.y - q.y;
        const distance = Math.sqrt(dx * dx + dy * dy);
        if (distance < linkDistance) {
          const alpha = (1 - distance / linkDistance) * .105;
          ctx.beginPath();
          ctx.moveTo(p.x, p.y);
          ctx.lineTo(q.x, q.y);
          ctx.strokeStyle = "rgba(83,190,226," + alpha + ")";
          ctx.lineWidth = .7;
          ctx.stroke();
        }
      }
    }
    raf = requestAnimationFrame(draw);
  }

  function start() {
    if (!raf && !document.hidden) raf = requestAnimationFrame(draw);
  }
  function stop() {
    if (raf) cancelAnimationFrame(raf);
    raf = 0;
  }

  resize();
  start();
  window.addEventListener("resize", resize, { passive: true });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stop();
    else start();
  });
})();
