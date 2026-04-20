let stations = [];
let currentStationId = null;
let pollInterval = null;

const searchInput = document.getElementById('station-search');
const searchResults = document.getElementById('search-results');
const arrivalsSection = document.getElementById('arrivals-section');
const arrivalsList = document.getElementById('arrivals-list');
const stationNameHeader = document.getElementById('current-station-name');
const refreshIndicator = document.getElementById('refresh-indicator');

// Initial load of stations
async function loadStations() {
    try {
        let url = '/stations?$top=1000&locationType=0';
        while (url) {
            const response = await fetch(url);
            const data = await response.json();
            
            if (data.value) {
                stations = stations.concat(data.value);
            }
            
            // Follow pagination link if present
            url = data["@odata.nextLink"];
            if (url) {
                console.log(`Loading more stations... current count: ${stations.length}`);
            }
        }
        console.log(`Loaded ${stations.length} total stations.`);
    } catch (err) {
        console.error('Error loading stations:', err);
    }
}

// Search filtering logic
searchInput.addEventListener('input', (e) => {
    const query = e.target.value.toLowerCase().trim();
    if (query.length < 2) {
        searchResults.classList.add('hidden');
        return;
    }

    const filtered = stations.filter(s => 
        s.name.toLowerCase().includes(query) || 
        s.id.toLowerCase().includes(query)
    ).slice(0, 10);

    if (filtered.length > 0) {
        searchResults.innerHTML = filtered.map(s => `
            <div data-id="${s.id}" data-name="${s.name}">
                <span class="result-name">${s.name}</span>
                <span class="result-id">#${s.id}</span>
            </div>
        `).join('');
        searchResults.classList.remove('hidden');
    } else {
        searchResults.classList.add('hidden');
    }
});

// Selection logic
searchResults.addEventListener('click', (e) => {
    const div = e.target.closest('div');
    if (!div) return;

    const id = div.dataset.id;
    const name = div.dataset.name;

    selectStation(id, name);
    searchInput.value = '';
    searchResults.classList.add('hidden');
});

function selectStation(id, name, updateHash = true) {
    currentStationId = id;
    stationNameHeader.textContent = name;
    arrivalsSection.classList.remove('hidden');

    if (updateHash) {
        window.location.hash = id;
    }
    
    // Clear previous polling
    if (pollInterval) clearInterval(pollInterval);
    
    fetchArrivals();
    pollInterval = setInterval(fetchArrivals, 15000); // 15 seconds
}

async function fetchArrivals() {
    if (!currentStationId) return;

    refreshIndicator.classList.add('refreshing');
    try {
        const response = await fetch(`/stations/${currentStationId}/arrivals?$top=20`);
        const data = await response.json();
        renderArrivals(data.value);
    } catch (err) {
        console.error('Error fetching arrivals:', err);
        arrivalsList.innerHTML = '<p class="error">Failed to load live data. Retrying...</p>';
    } finally {
        setTimeout(() => refreshIndicator.classList.remove('refreshing'), 500);
    }
}

function renderArrivals(arrivals) {
    const now = new Date();
    
    if (arrivals.length === 0) {
        arrivalsList.innerHTML = '<p class="no-data">No upcoming arrivals found for this station.</p>';
        return;
    }

    arrivalsList.innerHTML = arrivals.map(a => {
        const arrivalTime = new Date(a.arrivalTime || a.departureTime);
        const diffMs = arrivalTime - now;
        const diffMins = Math.floor(diffMs / 60000);
        
        let countdownText = '';
        let statusClass = '';

        if (diffMins <= 0) {
            countdownText = 'Arriving Now';
            statusClass = 'status-now';
        } else if (diffMins === 1) {
            countdownText = '1 min';
            statusClass = 'status-soon';
        } else {
            countdownText = `${diffMins} mins`;
        }

        const timeStr = arrivalTime.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });

        return `
            <div class="arrival-card">
                <div class="arrival-route">
                    <span class="route-id">${a.routeId}</span>
                    <span class="trip-id">${a.tripId.slice(-8)}</span>
                </div>
                <div class="arrival-time-container">
                    <div class="arrival-countdown ${statusClass}">${countdownText}</div>
                    <div class="arrival-status">${timeStr}</div>
                </div>
            </div>
        `;
    }).join('');
}

// Close search if clicking outside
document.addEventListener('click', (e) => {
    if (!searchInput.contains(e.target) && !searchResults.contains(e.target)) {
        searchResults.classList.add('hidden');
    }
});

async function checkHash() {
    const hash = window.location.hash.substring(1);
    if (hash && hash !== currentStationId) {
        try {
            const response = await fetch(`/stations/${hash}`);
            if (response.ok) {
                const data = await response.json();
                selectStation(data.id, data.name, false);
            }
        } catch (err) {
            console.error('Error loading station from hash:', err);
        }
    }
}

window.addEventListener('hashchange', checkHash);

// Initial load
loadStations();
checkHash();
