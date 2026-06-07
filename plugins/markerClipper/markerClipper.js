// markerClipper.js - UI integration for marker clipping plugin

(function() {
    'use strict';

    let sceneMarkers = []; // Cache for marker data

    // Load marker data for the current scene
    function loadSceneMarkers() {
        const sceneMatch = window.location.pathname.match(/\/scenes\/(\d+)/);
        if (!sceneMatch) {
            return Promise.resolve([]);
        }

        const sceneId = sceneMatch[1];

        return fetch('/graphql', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                query: `
                    query FindScene($id: ID!) {
                        findScene(id: $id) {
                            scene_markers {
                                id
                                title
                                seconds
                                end_seconds
                                primary_tag {
                                    id
                                    name
                                }
                            }
                        }
                    }
                `,
                variables: { id: sceneId }
            })
        })
        .then(response => response.json())
        .then(result => {
            if (result.errors) {
                console.error('GraphQL errors loading markers:', result.errors);
                return [];
            }

            sceneMarkers = result.data.findScene.scene_markers || [];
            console.log('Loaded', sceneMarkers.length, 'markers for scene');
            return sceneMarkers;
        })
        .catch(error => {
            console.error('Error loading scene markers:', error);
            return [];
        });
    }

    // Check if we're on a scene page with markers visible
    function isMarkersVisible() {
        const isScenePage = window.location.pathname.includes('/scenes/') &&
                          !window.location.pathname.endsWith('/scenes');
        const hasMarkersTab = document.querySelector('[data-cy="scene-markers-tab"], .markers-tab, .scene-markers') !== null;
        const isMarkersActive = document.querySelector('.nav-tabs .active, .tab-content .active')?.textContent?.includes('Markers') ||
                               document.querySelector('.markers-section, .scene-markers') !== null;

        return isScenePage && (hasMarkersTab || isMarkersActive);
    }

    // Add clip buttons to all markers (with or without end time)
    function addClipButtons() {
        if (!isMarkersVisible()) {
            return;
        }

        const cards = document.querySelectorAll('.primary-card, .card');
        cards.forEach(card => {
            const header = card.querySelector('h3');
            if (!header) return;
            const primaryTagName = header.textContent.trim();

            const cardBody = card.querySelector('.primary-card-body, .card-body');
            if (!cardBody) return;

            const markerDivs = Array.from(cardBody.children).filter(div =>
                div.querySelector('hr') && div.querySelector('.d-flex')
            );

            const markersForTag = sceneMarkers.filter(m =>
                m.primary_tag?.name === primaryTagName
            );

            markerDivs.forEach((markerDiv, idx) => {
                if (markerDiv.querySelector('.marker-clipper-btn')) return;

                // Assign data-marker-id for all markers (including those without end_seconds)
                if (idx < markersForTag.length) {
                    markerDiv.setAttribute('data-marker-id', markersForTag[idx].id);
                }

                const dFlex = markerDiv.querySelector('.d-flex');
                const loopBtn = dFlex ? dFlex.querySelector('button.btn-link') : null;

                const clipButton = document.createElement('button');
                clipButton.className = 'btn btn-sm btn-outline-secondary marker-clipper-btn ml-1';
                clipButton.innerHTML = '✂️';
                clipButton.title = 'Extract video clip from this marker';

                clipButton.addEventListener('click', function(e) {
                    e.preventDefault();
                    clipMarker(loopBtn || clipButton); // pass any button in the row for context
                });

                if (loopBtn) {
                    loopBtn.insertAdjacentElement('afterend', clipButton);
                } else if (dFlex) {
                    // Insert right after the timestamp div for consistent position
                    const timestampDiv = dFlex.querySelector('div');
                    if (timestampDiv) {
                        timestampDiv.insertAdjacentElement('afterend', clipButton);
                    } else {
                        dFlex.appendChild(clipButton);
                    }
                }
            });
        });
    }

    // Handle marker clipping
    async function clipMarker(loopButton) {
        // Extract marker identification info
        const markerInfo = await getMarkerInfo(loopButton);
        console.log('Extracted marker info:', markerInfo);
        if (!markerInfo) {
            alert('Could not determine marker information');
            return;
        }

        // Show loading state - find the clip button in the same container as the loop button
        const container = loopButton.parentElement;
        const button = container.querySelector('.marker-clipper-btn');
        if (!button) {
            alert('Could not find clip button');
            return;
        }

        const originalText = button.innerHTML;
        const originalClasses = button.className;
        button.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
        button.disabled = true;
        button.classList.remove('marker-clipper-success', 'marker-clipper-error');

        callPluginAPI('clip_marker', markerInfo)
            .then(result => {
                let response = null;
                if (result) {
                    try { response = JSON.parse(result); } catch (_) {}
                }

                if (response && response.message && !response.success) {
                    showButtonFeedback(button, 'error', '✕');
                    console.error('Clip task failed:', response.message);
                } else {
                    showButtonFeedback(button, 'success', '✓');
                    console.log('Clip task submitted');
                }
            })
            .catch(error => {
                console.error('Clip marker error:', error);
                showButtonFeedback(button, 'error', '✕');
            })
            .finally(() => {
                // Restore after short delay if not already showing feedback
                setTimeout(() => {
                    if (!button.classList.contains('marker-clipper-success') && !button.classList.contains('marker-clipper-error')) {
                        button.innerHTML = originalText;
                        button.className = originalClasses;
                        button.disabled = false;
                    }
                }, 1500);
            });
    }

    function showButtonFeedback(button, state, icon) {
        button.innerHTML = icon;
        button.classList.remove('marker-clipper-success', 'marker-clipper-error');
        button.classList.add(state === 'success' ? 'marker-clipper-success' : 'marker-clipper-error');
        button.disabled = true;

        setTimeout(() => {
            button.innerHTML = '✂️';
            button.classList.remove('marker-clipper-success', 'marker-clipper-error');
            button.disabled = false;
        }, 2000);
    }

    // Extract marker information from container
    function getMarkerInfo(loopButton) {
        // Get scene ID from URL
        const sceneMatch = window.location.pathname.match(/\/scenes\/(\d+)/);
        if (!sceneMatch) {
            return null;
        }
        const sceneId = sceneMatch[1];

        // Find the marker div that contains this loop button
        // The marker div has the data-marker-id attribute we added
        let markerDiv = loopButton.closest('[data-marker-id]');

        if (markerDiv) {
            const markerId = markerDiv.getAttribute('data-marker-id');
            if (markerId) {
                console.log('Found marker ID from data attribute:', markerId);
                return {
                    scene_id: sceneId,
                    marker_id: markerId
                };
            }
        }

        // Fallback: if we can't find the data attribute, try to find the marker div by traversing up
        // and looking for a div that contains a Loop button
        markerDiv = loopButton.closest('div');
        while (markerDiv && markerDiv.parentElement) {
            // Check if this div contains the Loop button and looks like a marker div
            if (markerDiv.querySelector('button') &&
                markerDiv.textContent.includes('Loop')) {
                break;
            }
            markerDiv = markerDiv.parentElement.closest('div');
        }

        if (markerDiv) {
            // Try to find the marker by matching the Loop button's position
            const allLoopButtons = document.querySelectorAll('button');
            const loopButtons = Array.from(allLoopButtons).filter(btn =>
                btn.textContent.trim() === 'Loop' &&
                btn.classList.contains('btn-link')
            );
            const buttonIndex = loopButtons.indexOf(loopButton);

            if (buttonIndex >= 0 && buttonIndex < sceneMarkers.length) {
                const marker = sceneMarkers[buttonIndex];
                console.log('Found marker by button position fallback:', marker.id, 'at index', buttonIndex);
                return {
                    scene_id: sceneId,
                    marker_id: marker.id
                };
            }
        }

        console.warn('Could not find marker ID - data attribute missing and fallback failed');
        return null;
    }



    // Call plugin API via GraphQL
    function callPluginAPI(mode, args = {}) {
        return fetch('/graphql', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                query: `mutation RunPluginOperation($plugin_id:ID!,$args:Map!){runPluginOperation(plugin_id:$plugin_id,args:$args)}`,
                variables: {
                    plugin_id: "markerClipper",
                    args: { mode: mode, ...args }
                }
            })
        })
        .then(response => response.json())
        .then(result => {
            console.log('GraphQL result:', result);
            if (result.errors) {
                console.error('GraphQL errors for RunPluginOperation:', result.errors);
                throw new Error(result.errors[0].message);
            }
            console.log('runPluginOperation result:', result.data.runPluginOperation);
            return result.data.runPluginOperation;
        });
    }

    // Initialize when DOM is ready
    async function init() {
        // Load marker data for the current scene
        await loadSceneMarkers();

        // Add buttons to existing markers
        addClipButtons();

        // Watch for dynamic content changes
        const observer = new MutationObserver(function(mutations) {
            mutations.forEach(function(mutation) {
                if (mutation.type === 'childList') {
                    addClipButtons();
                }
            });
        });

        observer.observe(document.body, {
            childList: true,
            subtree: true
        });

        // Handle SPA navigation (popstate + history API)
        let currentPath = window.location.pathname;
        function handleNavigation() {
            if (window.location.pathname !== currentPath) {
                currentPath = window.location.pathname;
                if (window.location.pathname.includes('/scenes/')) {
                    loadSceneMarkers().then(() => addClipButtons());
                }
            }
        }

        window.addEventListener('popstate', handleNavigation);

        const origPush = history.pushState;
        history.pushState = function(...args) {
            origPush.apply(this, args);
            handleNavigation();
        };

        const origReplace = history.replaceState;
        history.replaceState = function(...args) {
            origReplace.apply(this, args);
            handleNavigation();
        };
    }

    // Initialize on page load
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

})();