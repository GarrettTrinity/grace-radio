let currentMediaId = null;
let isPlaying = false;
let userInteracted = false;
let userManuallyStopped = false;
let currentLyrics = []; // Synced Lyrics Data // Flag to prevent auto-resync
let serverTimeOffset = 0; // Local - Server
let lastPlayRequestTime = 0; // Timestamp of last manual play

// --- Navigation ---
function switchTab(tabId) {
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    document.querySelectorAll('.menu-btn').forEach(b => b.classList.remove('active'));

    document.getElementById(tabId).classList.add('active');

    // Highlight button
    const map = {
        'player-view': 0,
        'library-view': 1,
        'schedule-view': 2
    };
    const index = map[tabId];
    if (index !== undefined) {
        document.querySelectorAll('.menu-btn')[index].classList.add('active');
    }

    if (tabId === 'library-view') fetchLibrary();
    if (tabId === 'schedule-view') fetchSchedule();
    if (tabId === 'stats-view') fetchStats();
}

// --- Player Logic ---
// --- Player Logic (Web Audio API) ---
const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
let decks = [];
let audioInitialized = false;
let activeDeckIndex = 0; // 0 or 1
let crossfadeDuration = 3; // Default

function updateCrossfade(val) {
    crossfadeDuration = parseFloat(val);
    const span = document.getElementById('cf-val');
    if (span) span.innerText = crossfadeDuration + 's';
}

function initDecks() {
    if (decks.length > 0) return;
    try {
        decks = [setupDeck('radio-audio'), setupDeck('radio-audio-2')];
    } catch (e) { console.error("Deck Init Failed", e); }
}

// Eager Initialization for Mobile Compat
document.addEventListener('DOMContentLoaded', initDecks);

function initAudio() {
    if (audioInitialized) return;
    try {
        initDecks();
        audioCtx.resume();

        // UNLOCK MOBILE AUDIO: Force localized play/pause
        decks.forEach(d => {
            d.el.load(); // Refresh state
            const p = d.el.play();
            if (p) p.then(() => d.el.pause()).catch(e => { }); // Silent catch
        });

        audioInitialized = true;

        if ('mediaSession' in navigator) {
            navigator.mediaSession.setActionHandler('play', () => {
                const deck = decks[activeDeckIndex];
                if (deck) {
                    deck.el.play();
                    if (audioCtx.state === 'suspended') audioCtx.resume();
                }
            });
            navigator.mediaSession.setActionHandler('pause', () => {
                const deck = decks[activeDeckIndex];
                if (deck) deck.el.pause();
            });
        }
    } catch (e) { console.error("Audio Init Error (Refresh page if stuck):", e); }
}

function setupDeck(id) {
    const el = document.getElementById(id);
    if (!el) throw new Error("Audio Element Missing: " + id);
    const isMobile = /iPhone|iPad|iPod|Android/i.test(navigator.userAgent);

    let source = null;
    let low = null;
    let mid = null;
    let high = null;
    let gain = null;

    if (!isMobile) {
        source = audioCtx.createMediaElementSource(el); // Only hijack on Desktop

        // EQ Chain
        low = audioCtx.createBiquadFilter(); low.type = 'lowshelf'; low.frequency.value = 320;
        mid = audioCtx.createBiquadFilter(); mid.type = 'peaking'; mid.frequency.value = 1000; mid.Q.value = 0.5;
        high = audioCtx.createBiquadFilter(); high.type = 'highshelf'; high.frequency.value = 3200;

        gain = audioCtx.createGain();
        preAmp = audioCtx.createGain();

        source.connect(preAmp).connect(low).connect(mid).connect(high).connect(gain).connect(audioCtx.destination);
    } else {
        console.log("Mobile detected: Native Audio Mode (No Web Audio Graph)");
    }

    // Event Listeners
    el.onplay = () => { isPlaying = true; };
    el.onpause = () => {
        if (decks[activeDeckIndex] && decks[activeDeckIndex].el === el) isPlaying = false;
    };
    el.onerror = (e) => console.error("Deck Error", e);

    // Mobile Chain Fix: When one ends, immediately try to sync next
    el.onended = () => {
        // Fix: Ignore 'ended' event if this deck is not the active one (e.g. fading out)
        if (decks[activeDeckIndex] && decks[activeDeckIndex].el !== el) return;

        // Fix: Ignore silence unlock track ending
        if (el.src && el.src.startsWith("data:")) return;

        console.log("Track Ended (Active Deck). Force Sync.");
        currentMediaId = null; // Force refresh detection
        updateStatus(); // Immediate call
    };

    el.ontimeupdate = () => {
        if (!decks.length) return;
        const deck = decks[activeDeckIndex];
        if (deck.el !== el) return; // Only update UI for active deck

        const dur = el.duration;
        const cur = el.currentTime;
        if (dur && !isNaN(dur) && dur > 0) {
            // Trim visualization
            const tStart = deck.trimStart || 0;
            const tEnd = deck.trimEnd || dur;

            let effDur = tEnd - tStart;
            if (effDur <= 0) effDur = dur; // Fallback

            let effCur = cur - tStart;
            if (effCur < 0) effCur = 0;
            if (effCur > effDur) effCur = effDur;

            const pct = (effCur / effDur) * 100;
            const bar = document.getElementById('progress-bar');
            if (bar) bar.style.width = pct + '%';

            const c = document.getElementById('current-time');
            if (c) c.innerText = formatTime(effCur);

            const t = document.getElementById('total-time');
            if (t) t.innerText = formatTime(effDur);
        }
    };

    return { el, source, low, mid, high, gain, preAmp, currentId: null };
}

// Auto-Init on first interaction
document.addEventListener('click', function initOnFirstClick() {
    if (!audioInitialized) {
        initAudio();
        // Try to start if we have state
        if (currentMediaId) syncStream();
    }
    document.removeEventListener('click', initOnFirstClick);
}, { once: true });
document.addEventListener('touchstart', function initOnFirstTouch() {
    if (!audioInitialized) {
        initAudio();
        if (currentMediaId) syncStream();
    }
    document.removeEventListener('touchstart', initOnFirstTouch);
}, { once: true });

function togglePlayStop() {
    userInteracted = true;
    const btn = document.getElementById('sync-btn');

    if (!audioInitialized) {
        initAudio();
    }

    // Check UI state for intent
    const isCurrentlyStop = btn.innerText.includes("Stop");

    if (isCurrentlyStop) {
        // User wants to STOP
        userManuallyStopped = true;

        decks.forEach(d => {
            d.el.pause();
            // Do not clear src, just rewind. Clearing src breaks mobile resume.
            // d.el.src = ""; 
            try { d.el.currentTime = 0; } catch (e) { }
        });
        currentMediaId = null;

        // Immediate UI Update
        btn.innerText = "▶ Play";
        btn.title = "Start Playback";
        btn.classList.add('primary');
        btn.style.background = '';
    } else {
        // User wants to PLAY
        userManuallyStopped = false; // Reset flag
        lastPlayRequestTime = Date.now(); // Mark intent

        btn.innerText = "■ Stop";
        btn.title = "Stop Playback";
        btn.classList.remove('primary');
        btn.style.background = '#ff4444';

        // Direct interaction play (Critical for Mobile Resume)
        if (decks.length && decks[activeDeckIndex]) {
            const deck = decks[activeDeckIndex];

            // OPTIMISTIC START: Immediately play the stream endpoint
            // This satisfies the user gesture requirement without waiting for fetch
            const streamUrl = "/api/stream/current?t=" + Date.now();

            // Only force reload if empty or different
            if (!deck.el.src || deck.el.src.includes('data:audio') || !deck.el.src.includes('api/stream')) {
                console.log("Starting Optimistic Playback via Stream Endpoint");
                deck.el.src = streamUrl;
            }

            deck.el.play().catch(e => {
                console.warn("Manual Play Trigger Failed:", e);
                alert("Audio Play Failed. Please interact with the page and try again.");
            });
        }

        // Trigger sync
        updateStatus();
    }
}

// Deprecated but kept for compatibility logic reuse if needed
function syncStream() {
    togglePlayStop();
}

function toggleMute() {
    if (!decks.length) initAudio();
    const muted = !decks[0].el.muted;
    decks.forEach(d => d.el.muted = muted);

    const btn = document.getElementById('mute-btn');
    btn.innerText = muted ? 'Unmute' : 'Mute';
    btn.className = muted ? 'control-btn' : 'control-btn primary';
}

function setVolume(val) {
    if (decks.length) decks.forEach(d => d.el.volume = val);
}

function getListenerId() {
    let id = localStorage.getItem('grace_listener_id');
    if (!id) {
        id = 'lid-' + Math.random().toString(36).substr(2, 9) + Date.now().toString(36);
        localStorage.setItem('grace_listener_id', id);
    }
    return id;
}

async function updateStatus() {
    try {
        const res = await fetch('/api/status?t=' + Date.now(), {
            headers: { 'X-Listener-ID': getListenerId() }
        });
        const data = await res.json();
        const state = data.current_track;
        const queueList = data.queue || [];
        const listeners = data.listeners || 0;

        const lc = document.getElementById('listener-count');
        if (lc) lc.innerText = listeners;

        updatePlayerUI(state, queueList, data.user_vote);
        if (state) updateMediaSession(state);

        if (state && data.playing) {
            state.elapsed = data.elapsed;
            if (!userManuallyStopped) {
                if (audioCtx.state === 'suspended') audioCtx.resume();
                handleAudioSync(state);
            }
        } else {
            // Pause all
            if (decks.length) decks.forEach(d => d.el.pause());
            currentMediaId = null;
        }

    } catch (e) {
        console.error(e);
    } finally {
        setTimeout(updateStatus, 1000);
    }
}

function updatePlayerUI(state, queueList, userVote) {
    const title = document.getElementById('current-title');
    const category = document.getElementById('current-category');
    const progressBar = document.getElementById('progress-bar');
    const timeCur = document.getElementById('current-time');
    const timeTot = document.getElementById('total-time');
    const art = document.getElementById('current-art');
    const initials = document.getElementById('art-initials');

    if (!state) {
        title.innerText = "Waiting for broadcast...";
        category.innerText = "OFFLINE";
        progressBar.style.width = '0%';
        initials.innerText = "♫";
        // Reset votes
        document.querySelectorAll('.vote-btn').forEach(b => {
            b.classList.remove('active');
            b.disabled = false;
            b.innerHTML = b.getAttribute('data-original-text') || b.innerHTML;
        });
    } else {
        title.innerText = state.title;
        category.innerText = state.category;

        // Ensure Button State matches Reality (Logic Fix Refined)
        // If user manually stopped, DO NOT auto-change to Stop (which looks like Play in code logic if reversed)
        // Only override if we are NOT manually stopped.
        const btn = document.getElementById('sync-btn');
        const isPlayingAudio = decks.some(d => !d.el.paused);

        if (btn && !userManuallyStopped) {
            const btnIsStop = btn.innerText.includes("Stop");
            // If audio is playing but button says Play -> Fix it to Stop
            if (isPlayingAudio && !btnIsStop) {
                btn.innerText = "■ Stop";
                btn.classList.remove('primary');
                btn.style.background = '#ff4444';
            }
            // If audio is NOT playing but button says Stop -> Fix to Play?
            // Only if we expected it to be playing?
            // Actually, if it stopped on its own (buffer underrun?), we might want to show Play.
            else if (!isPlayingAudio && btnIsStop) {
                // Only revert to Play if enough time has passed (to allow buffering/unlocking)
                if (Date.now() - lastPlayRequestTime > 3000) {
                    btn.innerText = "▶ Play";
                    btn.classList.add('primary');
                    btn.style.background = '';
                }
            }
        }

        // Update Star Rating
        const starContainer = document.getElementById('vote-controls');
        if (starContainer) {
            const stars = starContainer.querySelectorAll('.star');
            const msg = document.getElementById('vote-msg');

            // Reset
            stars.forEach(s => s.classList.remove('active'));
            if (msg) msg.innerText = "Rate this track";

            if (userVote) {
                // Highlight stars up to vote
                // DOM is reversed (5,4,3,2,1) so we need to be careful OR querySelectorAll returns them in source order (5..1)
                // Actually source order is 5,4,3,2,1.
                // If I voted 4: I want 4,3,2,1 to be active.
                // Wait, visually left is 1?
                // CSS: flex-direction: row-reverse.
                // HTML: 5 4 3 2 1
                // Visual: 1 2 3 4 5
                // So if I click Visual 4 (Source 4), I want Visual 1,2,3,4 highlighted.
                // Those are Source 1,2,3,4.

                // Let's just use data-value
                stars.forEach(s => {
                    if (parseInt(s.getAttribute('data-value')) <= userVote) {
                        s.classList.add('active');
                    }
                });
                if (msg) msg.innerText = "You rated: " + userVote + " ★";
            }
        }


        // Update Art
        if (state.art) {
            art.style.backgroundImage = `url('${state.art}')`;
            art.style.backgroundSize = 'cover';
            art.style.backgroundPosition = 'center';
            initials.style.display = 'none';
        } else {
            art.style.backgroundImage = 'none';
            initials.innerText = state.category === 'Music' ? '♫' : (state.category === 'Sermon' ? '✝' : '📢');
            initials.style.display = 'block';
        }

        // Progress
        const duration = state.duration || 1;
        const elapsed = state.elapsed || 0;

        // Only update UI from server if NOT playing (to avoid jitter with local audio)
        if (!isPlaying) {
            const pct = Math.min(100, (elapsed / duration) * 100);
            progressBar.style.width = pct + '%';
            timeCur.innerText = formatTime(elapsed);
            timeTot.innerText = formatTime(duration);
        }
    }

    // Update Queue Preview
    // Update Queue Preview
    const qList = document.getElementById('active-queue');
    if (queueList.length > 0) {
        qList.innerHTML = queueList.map((item, idx) => `
            <div class="queue-item" style="padding:8px; border-bottom:1px solid #333; display:flex; justify-content:space-between; align-items:center;">
                <div style="flex:1; overflow:hidden; white-space:nowrap; text-overflow:ellipsis;">
                    <span style="color:#888; margin-right:5px;">${idx + 1}.</span> 
                    ${item.title}
                </div>
                <div style="display:flex; align-items:center;">
                     <span class="badge" style="font-size:0.7em; margin-right:5px;">${item.category}</span>
                     ${(typeof IS_ADMIN !== 'undefined' && IS_ADMIN) ?
                `<button type="button" onclick="removeFromQueue(event, '${item.id}')" style="background:none; border:none; color:#ff4444; cursor:pointer; font-weight:bold; padding:0 5px;" title="Remove">✕</button>`
                : ''}
                </div>
            </div>
        `).join('');
    } else {
        qList.innerHTML = `<p class="empty-state">Queue is empty. Shuffling playlist.</p>`;
    }
}

function updateMediaSession(state) {
    if ('mediaSession' in navigator) {
        navigator.mediaSession.metadata = new MediaMetadata({
            title: state.title || "Grace Radio",
            artist: state.category || "Live Broadcast",
            album: "Grace Radio",
            artwork: []
        });
    }
}

// Make global for inline onclick
window.removeFromQueue = async function (event, id) {
    if (event) event.stopPropagation();
    console.log("Removing queue item:", id);
    if (!confirm("Remove from Up Next?")) return;

    try {
        await fetch('/api/queue/remove', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id })
        });
        updateStatus(); // Refresh immediately
    } catch (e) {
        console.error(e);
        alert("Failed to remove item.");
    }
}

function handleAudioSync(state) {
    if (userManuallyStopped) return; // Block auto-play if user stopped
    if (!userInteracted || !decks.length) return;

    // Check for Track Change
    // Special Case: If we are playing the stream endpoint (optimistic start), 
    // we consider the track "loaded" effectively, unless the ID has ACTUALLY changed on server side
    // relative to what we THOUGHT we were playing. 
    // But since currentMediaId starts null, we need to be careful.

    // If active deck is playing stream URL, and we just started, adopt the new ID without reloading.
    const deck = decks[activeDeckIndex];
    if (currentMediaId === null && deck && deck.el && deck.el.src && deck.el.src.includes('/api/stream/current')) {
        console.log("Optimistic Stream detected. Adopting ID: " + state.id);
        currentMediaId = state.id;
    }

    if (currentMediaId !== state.id) {
        console.log("Crossfade Switch:", state.title || "Unknown");
        currentMediaId = state.id;

        // Load Lyrics (Safe wrapper to preventing blocking playback)
        try {
            const raw = state.lyrics || "";
            currentLyrics = parseLRC(raw);
            renderLyrics(currentLyrics, raw);
        } catch (e) { console.error("Lyrics Render Failed:", e); }

        const prevDeck = decks[activeDeckIndex];
        activeDeckIndex = (activeDeckIndex + 1) % 2;
        const nextDeck = decks[activeDeckIndex];

        // Prepare URL
        let url = `/static/media/${state.filename.replace(/\\/g, '/')}`;
        if (url.indexOf('?') === -1) url += '?t=' + Date.now();

        // Logic for Trim
        const trimStart = state.trim_start || 0;
        nextDeck.trimStart = trimStart;
        nextDeck.trimEnd = state.trim_end || state.duration;

        // Setup Playback Target
        const targetTime = trimStart + state.elapsed;

        // Fix: Robust Seek Strategy (Immediate + Async)
        const seekHandler = () => {
            // Only seek if we are far off (avoid stutter if already there)
            if (Math.abs(nextDeck.el.currentTime - targetTime) > 0.5) {
                console.log(`Seek Handler: Jumping to ${targetTime}s`);
                nextDeck.el.currentTime = targetTime;
            }
        };

        // Attach listeners BEFORE setting src to catch all events
        nextDeck.el.addEventListener('loadedmetadata', seekHandler, { once: true });
        nextDeck.el.addEventListener('canplay', seekHandler, { once: true });

        // Set Source
        nextDeck.el.src = url;
        nextDeck.el.load();

        // Attempt Immediate Seek (for cached files)
        nextDeck.el.currentTime = targetTime;

        if (nextDeck.preAmp) {
            nextDeck.preAmp.gain.value = (state.volume !== undefined && state.volume !== null) ? state.volume : 1.0;
        }

        // CROSSFADE LOGIC
        const now = audioCtx.currentTime;
        const fadeDur = crossfadeDuration;

        // Fade OUT Previous (if playing)
        if (!prevDeck.el.paused) {
            if (prevDeck.gain && prevDeck.gain.gain) {
                // Desktop: Gain Node
                prevDeck.gain.gain.cancelScheduledValues(now);
                prevDeck.gain.gain.setValueAtTime(1, now);
                prevDeck.gain.gain.linearRampToValueAtTime(0, now + fadeDur);
            } else {
                // Mobile: No Gain Node
            }

            setTimeout(() => {
                prevDeck.el.pause();
                prevDeck.el.src = ""; // Clear buffer
                if (prevDeck.gain && prevDeck.gain.gain) prevDeck.gain.gain.value = 1; // Reset
            }, fadeDur * 1000 + 100);
        }

        // Fade IN Next
        if (nextDeck.gain && nextDeck.gain.gain) {
            // Desktop: Gain Node Crossfade
            nextDeck.gain.gain.cancelScheduledValues(now);
            nextDeck.gain.gain.setValueAtTime(0, now);
            nextDeck.gain.gain.linearRampToValueAtTime(1, now + fadeDur);
        } else {
            // Mobile: No Gain Node, just play. Volume is on element.
            // We can try volume ramping if we want, but simple is better for now.
            nextDeck.el.volume = 1;
        }

        nextDeck.el.play().catch(e => console.error("Play failed", e));

    } else {
        // Same Track - Live Update Props
        const deck = decks[activeDeckIndex];
        if (deck) {
            deck.trimStart = state.trim_start || 0;
            deck.trimEnd = state.trim_end || state.duration;
            if (state.volume !== undefined && deck.preAmp) {
                deck.preAmp.gain.value = state.volume;
            }
            if (state.lyrics !== undefined) {
                const raw = state.lyrics || "";
                currentLyrics = parseLRC(raw);
                renderLyrics(currentLyrics, raw);
            }

            // AUTO-RESUME: If we should be playing but aren't
            if (deck && deck.el.paused && !deck.el.ended && deck.el.error === null) {
                console.warn("Sync: Track matches but deck is paused. Resuming...");
                const playPromise = deck.el.play();
                if (playPromise !== undefined) {
                    playPromise.catch(e => console.error("Auto-Resume failed:", e));
                }
            }
        }

        // Drifting check?
        // This check should apply to the currently active deck
        if (deck && !deck.el.paused && Math.abs(deck.el.currentTime - state.elapsed) > 8) {
            console.log("Resyncing time...");
            deck.el.currentTime = state.elapsed;
        }
    }

    // Apply Live EQ (Always, for listeners if supported)
    const deck = decks[activeDeckIndex];
    if (deck && deck.low && deck.mid && deck.high) {
        const eq = state.eq || { low: 0, mid: 0, high: 0 };
        const safeVal = (v) => Math.max(-10, Math.min(10, v || 0));
        const now = audioCtx.currentTime;
        // setTargetAtTime avoids clicks
        deck.low.gain.setTargetAtTime(safeVal(eq.low), now, 0.2);
        deck.mid.gain.setTargetAtTime(safeVal(eq.mid), now, 0.2);
        deck.high.gain.setTargetAtTime(safeVal(eq.high), now, 0.2);
    }
}


// --- EQ UI Handlers ---
function openEQModal() {
    if (!activeDeckIndex && activeDeckIndex !== 0) {
        // Could happen if no audio yet
        initAudio();
    }
    const deck = decks[activeDeckIndex] || decks[0];
    if (!deck) return; // Should not happen

    // Get current vals if nodes exist
    if (deck.low) {
        document.getElementById('eq-low').value = deck.low.gain.value;
        document.getElementById('eq-mid').value = deck.mid.gain.value;
        document.getElementById('eq-high').value = deck.high.gain.value;
        if (deck.preAmp) {
            document.getElementById('eq-vol').value = deck.preAmp.gain.value;
        } else {
            document.getElementById('eq-vol').value = 1.0;
        }
    } else {
        // Mobile fallback - just show default
        document.getElementById('eq-low').value = 0;
        document.getElementById('eq-mid').value = 0;
        document.getElementById('eq-high').value = 0;
        document.getElementById('eq-vol').value = 1.0;
    }

    updateEQLabels();

    document.getElementById('eq-modal').style.display = 'block';
}

function closeEQModal() { document.getElementById('eq-modal').style.display = 'none'; }

// Live Update Labels
document.getElementById('eq-low').oninput = updateEQLabels;
document.getElementById('eq-mid').oninput = updateEQLabels;
document.getElementById('eq-high').oninput = updateEQLabels;
document.getElementById('eq-vol').oninput = updateEQLabels;

function updateEQLabels() {
    const low = document.getElementById('eq-low').value;
    const mid = document.getElementById('eq-mid').value;
    const high = document.getElementById('eq-high').value;
    const vol = document.getElementById('eq-vol').value;
    document.getElementById('val-low').innerText = low;
    document.getElementById('val-mid').innerText = mid;
    document.getElementById('val-high').innerText = high;
    document.getElementById('val-vol').innerText = vol;

    // Live Review: Apply to Active Deck
    if (decks.length) {
        const deck = decks[activeDeckIndex];
        if (deck.low) {
            deck.low.gain.value = low;
            deck.mid.gain.value = mid;
            deck.high.gain.value = high;
        }
        if (deck.preAmp) {
            deck.preAmp.gain.value = vol;
        }
    }
}

async function saveEQ() {
    if (!currentMediaId) return;
    const low = document.getElementById('eq-low').value;
    const mid = document.getElementById('eq-mid').value;
    const high = document.getElementById('eq-high').value;

    const settings = { low: parseFloat(low), mid: parseFloat(mid), high: parseFloat(high) };

    try {
        const res = await fetch('/api/library/update', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                id: currentMediaId,
                eq: settings,
                volume: parseFloat(document.getElementById('eq-vol').value)
            })
        });
        if (res.ok) {
            alert("EQ Saved for this track!");
            closeEQModal();
        } else alert("Failed to save EQ");
    } catch (e) { console.error(e); }
}

// Handle Loading Errors (e.g. 404, Format)


function formatTime(sec) {
    if (!sec || isNaN(sec)) return "0:00";
    sec = Math.floor(sec);
    let min = Math.floor(sec / 60);
    let s = sec % 60;
    return min + ':' + (s < 10 ? '0' : '') + s;
}

// --- Library ---
async function fetchLibrary() {
    const res = await fetch('/api/library');
    const data = await res.json();
    renderLibrary(data);
}

let allMedia = [];
let currentFilter = 'all';

// Batch State
let selectedItems = new Set();
let currentPath = "";
let cachedFolders = [];

function toggleSelection(id) {
    if (selectedItems.has(id)) selectedItems.delete(id);
    else selectedItems.add(id);
    renderLibrary(allMedia);
}

async function createNewFolder() {
    const name = prompt("Enter new folder name:");
    if (!name) return;
    if (selectedItems.size === 0) {
        alert("Please select tracks to move into the new folder first.");
        return;
    }
    await performBatchMove(name);
}

async function moveSelected() {
    if (selectedItems.size === 0) {
        alert("Select tracks first.");
        return;
    }
    const name = prompt("Enter target folder name (or leave empty to move to Root):");
    if (name === null) return;
    await performBatchMove(name);
}

async function performBatchMove(folderName) {
    const ids = Array.from(selectedItems);
    try {
        const res = await fetch('/api/library/batch_move', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ids, folder: folderName })
        });
        if (res.ok) {
            selectedItems.clear();
            alert("Moved items successfully.");
            fetchLibrary(); // Reload
        } else {
            alert("Move failed.");
        }
    } catch (e) { console.error(e); }
}

function navigateFolder(path) {
    currentPath = path;
    renderLibrary(allMedia);
}

function renderLibrary(data) {
    allMedia = data;
    const list = document.getElementById('library-list');
    list.innerHTML = '';

    // Update Breadcrumbs & Toolbar
    const crumbs = document.getElementById('lib-breadcrumbs');
    if (crumbs) {
        let html = '';
        if (!currentPath) html = `<span onclick="navigateFolder('')" style="cursor:pointer; color:#88f; font-weight:bold;">/ Root</span>`;
        else {
            const parts = currentPath.split('/').filter(p => p);
            html = `<span onclick="navigateFolder('')" style="cursor:pointer; color:#88f; font-weight:bold;">/ Root</span> <span style="opacity:0.5;">/ ${parts.join('/')}</span>`;
            let parent = parts.slice(0, -1).join('/');
            if (parent) parent += '/';
            html += ` <button onclick="navigateFolder('${parent}')" style="margin-left:20px; padding:2px 8px; cursor:pointer;">⬆ Up</button>`;
        }

        // Add Batch Controls
        if (typeof IS_ADMIN !== 'undefined' && IS_ADMIN) {
            html += `
                <div style="margin-left:auto; display:flex; gap:10px;">
                    <button onclick="createNewFolder()" class="btn-primary" style="padding:2px 10px; font-size:0.8rem;">+ New Folder</button>
                    ${selectedItems.size > 0 ? `<button onclick="moveSelected()" class="btn-card" style="padding:2px 10px; font-size:0.8rem;">Move (${selectedItems.size})</button>` : ''}
                </div>
            `;
        }
        crumbs.innerHTML = html;
    }

    const filtered = currentFilter === 'all' ? data : data.filter(d => d.category === currentFilter);

    // Grouping Logic
    const itemsInView = [];
    const foldersInView = new Set();

    filtered.forEach(item => {
        let textPath = (item.filename || '').replace(/\\/g, '/');
        // If currentPath is set, we expect prefix
        if (!currentPath || textPath.startsWith(currentPath)) {
            // Remove prefix to see relative path
            const relPath = currentPath ? textPath.substring(currentPath.length) : textPath;

            if (relPath.includes('/')) {
                // It is inside a subfolder relative to here
                const sub = relPath.split('/')[0];
                foldersInView.add(sub);
            } else {
                // It is a file in the current view
                itemsInView.push(item);
            }
        }
    });

    if (itemsInView.length === 0 && foldersInView.size === 0) {
        list.innerHTML = '<p style="grid-column: 1/-1; text-align: center; color: #666;">No media found.</p>';
        return;
    }

    // Render Folders
    Array.from(foldersInView).sort().forEach(f => {
        const card = document.createElement('div');
        card.className = 'media-card folder-card';
        card.style.background = '#222';
        card.style.border = '1px solid #444';
        card.style.cursor = 'pointer';
        card.title = `Open ${f}`;
        card.innerHTML = `
            <div style="font-size:2.5em; text-align:center; color:#eda;">📁</div>
            <h4 style="text-align:center; margin-top:5px; color:#fff;">${f}</h4>
         `;
        card.onclick = () => navigateFolder(currentPath + f + '/');
        list.appendChild(card);
    });

    // Render Files
    itemsInView.forEach(item => {
        const card = document.createElement('div');
        card.className = 'media-card';
        card.style.position = 'relative'; // Ensure absolute checkbox works
        const isSelected = selectedItems.has(item.id);
        if (isSelected) card.style.border = '1px solid #eda';

        // Buttons
        let buttons = '';
        if (typeof IS_ADMIN !== 'undefined' && IS_ADMIN) {
            buttons = `
                <button class="btn-card" onclick="queueItem('${item.id}')">Queue Next</button>
                <button class="btn-card" onclick="openScheduleModal('${item.id}', '${item.title.replace(/'/g, "&apos;")}')">Schedule</button>
                <button class="btn-card" onclick="openEditModalFromId('${item.id}')">Edit</button>
                <button class="btn-card" style="color:#ff4444" onclick="deleteItem('${item.id}')">Delete</button>
             `;
        } else {
            // Listener view
        }

        card.innerHTML = `
            ${(typeof IS_ADMIN !== 'undefined' && IS_ADMIN) ?
                `<input type="checkbox" ${isSelected ? 'checked' : ''} onclick="event.stopPropagation(); toggleSelection('${item.id}')" style="position:absolute; top:10px; left:10px; transform:scale(1.5); z-index:10; cursor:pointer;">` : ''}
            <h4 style="margin-top:20px;">${item.title}</h4>
            <p>${item.category} • ${formatTime(item.duration)}</p>
            <div class="card-actions">
                ${buttons}
            </div>
        `;
        list.appendChild(card);
    });
}

function filterLibrary(cat) {
    currentFilter = cat;
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    // cheap way to find btn
    event.target.classList.add('active');
    renderLibrary(allMedia);
}

async function queueItem(id) {
    await fetch('/api/queue/add', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id })
    });
    alert("Added to Queue (Priority)");
}

async function deleteItem(id) {
    if (!confirm("Are you sure?")) return;
    await fetch('/api/delete/' + id, { method: 'DELETE' });
    fetchLibrary();
}

// --- Upload ---
function openUploadModal() { document.getElementById('upload-modal').style.display = 'block'; }
function closeUploadModal() { document.getElementById('upload-modal').style.display = 'none'; }

document.getElementById('upload-form').onsubmit = async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const btn = e.target.querySelector('button');
    btn.innerText = "Uploading...";
    btn.disabled = true;

    try {
        const res = await fetch('/api/upload', {
            method: 'POST',
            body: fd
        });
        if (res.ok) {
            closeUploadModal();
            fetchLibrary();
            alert("Uploaded successfully!");
        } else {
            alert("Upload failed");
        }
    } catch (err) {
        alert("Error: " + err);
    }
    btn.innerText = "Upload";
    btn.disabled = false;
    e.target.reset();
};

// --- Schedule ---
// --- Schedule ---
function openScheduleModal(id, title) {
    document.getElementById('schedule-modal').style.display = 'block';
    document.getElementById('schedule-media-id').value = id;
    document.getElementById('schedule-item-title').innerText = title;
    // Clear Edit Mode
    document.getElementById('schedule-id').value = "";
    document.getElementById('schedule-time').value = "";
    document.querySelector('#schedule-form button').innerText = "Set Schedule";
}

function openEditScheduleModal(scheduleId, runAt, title, mediaId) {
    document.getElementById('schedule-modal').style.display = 'block';
    document.getElementById('schedule-media-id').value = mediaId;
    document.getElementById('schedule-item-title').innerText = "Reschedule: " + title;
    document.getElementById('schedule-id').value = scheduleId;

    // Convert timestamp (seconds) to datetime-local (YYYY-MM-DDTHH:MM)
    const d = new Date(runAt * 1000);
    // Adjust for timezone offset to show local time
    const iso = new Date(d.getTime() - (d.getTimezoneOffset() * 60000)).toISOString().slice(0, 16);
    document.getElementById('schedule-time').value = iso;
    document.querySelector('#schedule-form button').innerText = "Update Schedule";
}

function closeScheduleModal() { document.getElementById('schedule-modal').style.display = 'none'; }

document.getElementById('schedule-form').onsubmit = async (e) => {
    e.preventDefault();
    const mid = document.getElementById('schedule-media-id').value;
    const sid = document.getElementById('schedule-id').value;
    const timeStr = document.getElementById('schedule-time').value;

    if (!timeStr) return;

    // Convert Local DOM String to UTC Timestamp (Seconds)
    const runAt = new Date(timeStr).getTime() / 1000;

    if (sid) {
        // UPDATE
        await fetch('/api/schedule/update', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: sid, run_at: runAt })
        });
    } else {
        // ADD
        await fetch('/api/schedule/add', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: mid, run_at: runAt })
        });
    }

    closeScheduleModal();
    if (document.getElementById('schedule-view').classList.contains('active')) {
        fetchSchedule();
    }
    alert("Schedule Updated!");
};

async function fetchSchedule() {
    try {
        const res = await fetch('/api/schedule/list');
        const data = await res.json();

        const div = document.getElementById('schedule-list');
        if (data.length === 0) {
            div.innerHTML = "<p class='empty-state'>No upcoming broadcasts scheduled.</p>";
            return;
        }

        div.innerHTML = data.map(item => {
            const date = new Date(item.run_at * 1000).toLocaleString();
            return `
            <div class="media-card" style="display:flex; justify-content:space-between; align-items:center;">
                <div style="flex:1">
                    <div style="color:#00ffc8; font-size:0.9em; margin-bottom:5px;">🕒 ${date}</div>
                    <h4>${item.title}</h4>
                    <span class="badge">${item.category}</span>
                </div>
                <div style="display:flex; gap:10px;">
                    <button class="btn-card" onclick="openEditScheduleModal('${item.id}', ${item.run_at}, '${item.title.replace(/'/g, "&apos;")}', '${item.media_id}')">Edit Time</button>
                    <button class="btn-card" style="color:#ff4444" onclick="removeScheduleItem('${item.id}')">Remove</button>
                </div>
            </div>
            `;
        }).join('');
    } catch (e) { console.error(e); }
}

async function removeScheduleItem(id) {
    if (!confirm("Cancel this scheduled broadcast?")) return;
    await fetch('/api/schedule/remove', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id })
    });
    fetchSchedule();
}


// --- Init ---
setInterval(updateStatus, 1000);
updateStatus();

// --- YouTube ---
function openYoutubeModal() {
    document.getElementById('youtube-modal').style.display = 'block';
}
function closeYoutubeModal() {
    document.getElementById('youtube-modal').style.display = 'none';
}

const ytForm = document.getElementById('youtube-form');
if (ytForm) {
    ytForm.onsubmit = async (e) => {
        e.preventDefault();
        const url = document.getElementById('yt-url').value;
        // Use optional chaining just in case
        const catEl = document.getElementById('yt-category');
        const category = catEl ? catEl.value : 'Music';

        const btn = e.target.querySelector('button');
        btn.innerText = "Processing...";
        btn.disabled = true;

        try {
            const res = await fetch('/api/upload/youtube', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url, category })
            });
            if (res.ok) {
                closeYoutubeModal();
                const d = await res.json();
                if (res.status === 202) {
                    alert(d.message || "Download started in background.");
                } else {
                    alert("Imported successfully!");
                    fetchLibrary();
                }
            } else {
                const text = await res.text();
                try {
                    const d = JSON.parse(text);
                    alert("Import failed: " + (d.error || 'Unknown'));
                } catch (e) {
                    alert("Server Error (HTML): " + text.substring(0, 150));
                }
            }
        } catch (err) {
            alert("Error: " + err);
        }
        btn.innerText = "Import Audio";
        btn.disabled = false;
        e.target.reset();
    };
}

// --- Edit Modal Handlers ---
async function refreshFolderList() {
    try {
        const res = await fetch('/api/library/folders');
        const folders = await res.json();
        const dl = document.getElementById('folder-datalist');
        if (dl) {
            dl.innerHTML = folders.map(f => `<option value="${f}">`).join('');
        }
    } catch (e) { }
}

function openEditModalFromId(id) {
    // Find item in allMedia global
    const item = allMedia.find(m => String(m.id) === String(id));
    if (item) openEditModal(item);
    else alert("Error: Item not found in memory.");
}

function openEditModal(item) {
    try {
        console.log("Opening Edit for:", item);
        document.getElementById('edit-id').value = item.id;
        document.getElementById('edit-title').value = item.title;
        document.getElementById('edit-category').value = item.category || 'Music';
        document.getElementById('edit-trim-start').value = item.trim_start || '';
        document.getElementById('edit-trim-end').value = item.trim_end || '';
        document.getElementById('edit-lyrics').value = item.lyrics || '';

        // Extract Folder
        let fname = item.filename || '';
        fname = fname.replace(/\\/g, '/');
        const parts = fname.split('/');
        let folder = '';
        if (parts.length > 1) {
            folder = parts.slice(0, -1).join('/');
        }
        const folderInput = document.getElementById('edit-folder');
        if (folderInput) folderInput.value = folder;

        // Art Preview
        const preview = document.getElementById('current-art-preview');
        const artInput = document.getElementById('edit-art');
        if (artInput) artInput.value = ""; // Reset file

        if (item.art) {
            preview.innerHTML = `<img src="${item.art}" style="height:50px; border-radius:4px;"> <span style="font-size:0.8em; color:#aaa;">Current Art</span>`;
        } else {
            preview.innerHTML = ``;
        }

        refreshFolderList(); // Async fetch suggestions

        document.getElementById('edit-modal').style.display = 'block';
    } catch (e) {
        alert("CRITICAL EDIT ERROR: " + e.message);
        console.error(e);
    }
}
function closeEditModal() { document.getElementById('edit-modal').style.display = 'none'; }

const editForm = document.getElementById('edit-form');
if (editForm) {
    editForm.onsubmit = async (e) => {
        e.preventDefault();

        const fd = new FormData();
        fd.append('id', document.getElementById('edit-id').value);
        fd.append('title', document.getElementById('edit-title').value);
        fd.append('category', document.getElementById('edit-category').value);
        fd.append('folder', document.getElementById('edit-folder').value);
        fd.append('trim_start', document.getElementById('edit-trim-start').value);
        fd.append('trim_end', document.getElementById('edit-trim-end').value);
        fd.append('lyrics', document.getElementById('edit-lyrics').value);

        const artFile = document.getElementById('edit-art').files[0];
        if (artFile) {
            fd.append('art', artFile);
        }

        const submitBtn = editForm.querySelector('button[type="submit"]');
        const origText = submitBtn.innerText;
        submitBtn.innerText = "Saving...";
        submitBtn.disabled = true;

        try {
            const res = await fetch('/api/library/update', {
                method: 'POST',
                body: fd
            });
            if (res.ok) {
                const data = await res.json();

                // VERIFICATION
                const sentLyrics = document.getElementById('edit-lyrics').value || "";
                const savedLyrics = (data.item && data.item.lyrics) || "";

                if (sentLyrics !== savedLyrics) {
                    alert(`WARNING: Mismatch!\nSent: ${sentLyrics.length}\nSaved: ${savedLyrics.length}\nTry again?`);
                }

                closeEditModal();
                fetchLibrary();
            } else {
                const text = await res.text();
                alert("Error saving: " + text);
            }
        } catch (e) {
            console.error(e);
            alert("Network Error: " + e.message);
        } finally {
            submitBtn.innerText = origText;
            submitBtn.disabled = false;
        }

    };
}

const cookieForm = document.getElementById('cookies-form');

async function forceReset() {
    if (!confirm("This will force the radio to skip and reset. Do you want to proceed?")) return;
    await fetch('/api/danger/force_next', { method: 'POST' });
    alert("Reset signal sent. Wait 5 seconds...");
}

async function skipTrack() {
    // if (!confirm("Skip current track?")) return; // Optional confirmation
    try {
        await fetch('/api/skip', { method: 'POST' });
    } catch (e) {
        console.error(e);
    }
}

if (cookieForm) {
    cookieForm.onsubmit = async (e) => {
        e.preventDefault();
        const fd = new FormData(e.target);
        const btn = e.target.querySelector('button');
        btn.innerText = "Updating...";
        btn.disabled = true;

        try {
            const res = await fetch('/api/upload/cookies', {
                method: 'POST',
                body: fd
            });
            if (res.ok) {
                alert("Cookies updated! Try importing again.");
            } else {
                const text = await res.text();
                try {
                    const d = JSON.parse(text);
                    alert("Cookie update failed: " + (d.error || 'Unknown'));
                } catch (e) {
                    alert("Server Error (HTML): " + text.substring(0, 150));
                }
            }
        } catch (err) {
            alert("Error: " + err);
        }
        btn.innerText = "Update Cookies";
        btn.disabled = false;
        e.target.reset();
    };
}

async function clearStats() {
    if (!confirm("Are you sure you want to clear ALL voting data? This cannot be undone.")) return;
    if (!confirm("Confirm again: This will wipe all ratings from every listener.")) return;

    try {
        const res = await fetch('/api/stats/clear', { method: 'POST' });
        if (res.ok) {
            alert("All stats cleared.");
            fetchStats();
            document.querySelectorAll('.vote-btn, .star').forEach(b => b.classList.remove('active'));
            // Optionally force listener ID reset or specific API to clear their session ref?
            // The requirement says "They would have to vote their star rating again", which implies the backend cleared it.
        } else {
            alert("Failed to clear stats.");
        }
    } catch (e) {
        console.error(e);
    }
}

// --- Voting System (Star Rating) ---
async function sendVote(rating) {
    if (!currentMediaId) {
        alert("Nothing is playing right now!");
        return;
    }

    try {
        await fetch('/api/vote', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-Listener-ID': getListenerId()
            },
            body: JSON.stringify({
                id: currentMediaId,
                rating: rating
            })
        });

        // Optimistic UI Update
        const starContainer = document.getElementById('vote-controls');
        const stars = starContainer.querySelectorAll('.star');
        stars.forEach(s => s.classList.remove('active'));
        stars.forEach(s => {
            if (parseInt(s.getAttribute('data-value')) <= rating) {
                s.classList.add('active');
            }
        });
        const msg = document.getElementById('vote-msg');
        if (msg) msg.innerText = "You rated: " + rating + " ★";

        // Background sync
        updateStatus();

    } catch (e) {
        console.error(e);
    }
}

let statsSort = 'average'; // average, votes, title, category

async function fetchStats(sortBy) {
    if (sortBy) statsSort = sortBy;

    const table = document.getElementById('stats-list');
    if (!table) return;

    table.innerHTML = '<tr><td colspan="5" style="text-align:center;">Loading data...</td></tr>';

    try {
        const res = await fetch('/api/stats/votes');
        let data = await res.json();

        if (data.length === 0) {
            table.innerHTML = '<tr><td colspan="5" style="text-align:center;">No votes recorded yet.</td></tr>';
            return;
        }

        // Client-side Sort
        data.sort((a, b) => {
            let valA = a[statsSort];
            let valB = b[statsSort];
            if (typeof valA === 'string') {
                return valA.localeCompare(valB);
            }
            if (statsSort === 'title' || statsSort === 'category') {
                return valA.localeCompare(valB);
            }
            return valB - valA; // Descending for numbers
        });

        let html = '';
        data.forEach(item => {
            // Color code average
            let color = '#888';
            if (item.average >= 4.5) color = '#00ffc8';
            else if (item.average >= 3.5) color = '#aaff00';
            else if (item.average >= 2.5) color = '#ffda00';
            else if (item.average < 2.5) color = '#ff4444';

            html += `
                <tr>
                    <td>${item.title}</td>
                    <td><span class="badge" style="font-size:0.8em; padding:2px 6px; background:#444; border-radius:4px;">${item.category}</span></td>
                    <td style="color:#00ffc8">${item.stars_5 || 0}</td>
                    <td style="color:#aaff00">${item.stars_4 || 0}</td>
                    <td style="color:#ffda00">${item.stars_3 || 0}</td>
                    <td style="color:#ff9900">${item.stars_2 || 0}</td>
                    <td style="color:#ff4444">${item.stars_1 || 0}</td>
                    <td style="text-align:center;">${item.votes}</td>
                    <td style="font-weight:bold; font-size:1.1rem; color:${color};">${item.average} ★</td>
                </tr>
            `;
        });
        table.innerHTML = html;

    } catch (e) {
        table.innerHTML = '<tr><td colspan="5" style="text-align:center; color:red;">Error loading stats</td></tr>';
    }
}

// --- Lyrics System ---
function openLyricsModal() {
    document.getElementById('lyrics-modal').style.display = 'block';
    // Scroll to active line
    if (currentLyrics.length) updateLyricsUI(decks[activeDeckIndex].el.currentTime, true);
}
function closeLyricsModal() {
    document.getElementById('lyrics-modal').style.display = 'none';
}

function parseLRC(text) {
    if (!text) return [];
    text = text.replace(/\r\n/g, '\n');
    const lines = text.split('\n');
    // Regex for [mm:ss.xx]
    const regex = /\[(\d{1,2}):(\d{1,2})(?:\.(\d{1,3}))?\](.*)/;
    const result = [];
    for (const line of lines) {
        const match = line.match(regex);
        if (match) {
            const min = parseInt(match[1]);
            const sec = parseInt(match[2]);
            const msStr = match[3] || "0";
            // if ms is 2 digits, it usually means centiseconds (x10). If 3, ms.
            const ms = parseInt(msStr.padEnd(3, '0').substring(0, 3));

            const time = min * 60 + sec + (ms / 1000);
            const content = match[4].trim();
            if (content) result.push({ time, text: content });
        }
    }
    return result.sort((a, b) => a.time - b.time);
}

function renderLyrics(lyrics, rawText = "") {
    const container = document.getElementById('lyrics-container');
    if (!lyrics || lyrics.length === 0) {
        if (rawText && rawText.trim().length > 0) {
            // Fallback: Display raw text
            document.getElementById('lyrics-title').innerText = "Lyrics (Unsynced)";
            container.innerHTML = rawText.split('\n').map(line =>
                `<p style="margin:10px 0; color:#aaa;">${line}</p>`
            ).join('');
        } else {
            container.innerHTML = '<p style="color:#666; margin-top:50px;">No synced lyrics available.<br><small>Add them in Edit Track menu (LRC format).</small></p>';
            document.getElementById('lyrics-title').innerText = "Lyrics (Unsynced)";
        }
        return;
    }
    document.getElementById('lyrics-title').innerText = "Lyrics";
    container.innerHTML = lyrics.map((l, i) =>
        `<p id="lyric-line-${i}" class="lyric-line" style="margin:10px 0; transition:all 0.3s ease; color:#666; cursor:pointer;" onclick="seekToLyric(${l.time})">${l.text}</p>`
    ).join('');
}

function seekToLyric(time) {
    if (decks[activeDeckIndex]) {
        decks[activeDeckIndex].el.currentTime = time;
        // Optionally update progress bar immediately
    }
}

function updateLyricsUI(currentTime, forceScroll = false) {
    if (document.getElementById('lyrics-modal').style.display === 'none') return;

    // Find active line
    let activeIdx = -1;
    for (let i = 0; i < currentLyrics.length; i++) {
        if (currentTime >= currentLyrics[i].time) {
            activeIdx = i;
        } else {
            break;
        }
    }

    if (activeIdx !== -1) {
        const activeId = `lyric-line-${activeIdx}`;
        // Optimization: Don't re-query everything if ID matches last active?
        // But simpler to just loop class updates or id lookup
        const lines = document.getElementsByClassName('lyric-line');
        for (let l of lines) {
            l.style.color = '#666';
            l.style.transform = 'scale(1)';
            l.style.fontWeight = 'normal';
            l.style.textShadow = 'none';
        }

        const el = document.getElementById(activeId);
        if (el) {
            el.style.color = '#fff';
            el.style.transform = 'scale(1.1)';
            el.style.fontWeight = 'bold';
            el.style.textShadow = '0 0 10px rgba(0,255,100,0.5)';

            if (forceScroll || isPlaying) { // Only auto scroll if playing
                el.scrollIntoView({ behavior: 'smooth', block: 'center' });
            }
        }
    }
}
