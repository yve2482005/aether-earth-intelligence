/* AETHER offline gazetteer — ~40 major cities for map search.
   Name, country, lat, lon. No network needed. */
(function () {
  'use strict';
  const A = (window.AETHER = window.AETHER || {});
  A.gazetteer = [
    { n: 'Yangon', c: 'Myanmar', lat: 16.84, lon: 96.17 },
    { n: 'Mandalay', c: 'Myanmar', lat: 21.98, lon: 96.08 },
    { n: 'Nay Pyi Taw', c: 'Myanmar', lat: 19.76, lon: 96.07 },
    { n: 'Bangkok', c: 'Thailand', lat: 13.76, lon: 100.50 },
    { n: 'Chiang Mai', c: 'Thailand', lat: 18.79, lon: 98.99 },
    { n: 'Phuket', c: 'Thailand', lat: 7.88, lon: 98.41 },
    { n: 'Hanoi', c: 'Vietnam', lat: 21.03, lon: 105.85 },
    { n: 'Ho Chi Minh City', c: 'Vietnam', lat: 10.82, lon: 106.63 },
    { n: 'Vientiane', c: 'Laos', lat: 17.98, lon: 102.63 },
    { n: 'Phnom Penh', c: 'Cambodia', lat: 11.56, lon: 104.93 },
    { n: 'Kuala Lumpur', c: 'Malaysia', lat: 3.14, lon: 101.69 },
    { n: 'Singapore', c: 'Singapore', lat: 1.35, lon: 103.82 },
    { n: 'Jakarta', c: 'Indonesia', lat: -6.21, lon: 106.85 },
    { n: 'Manila', c: 'Philippines', lat: 14.60, lon: 120.98 },
    { n: 'Dhaka', c: 'Bangladesh', lat: 23.81, lon: 90.41 },
    { n: 'Kolkata', c: 'India', lat: 22.57, lon: 88.36 },
    { n: 'Chennai', c: 'India', lat: 13.08, lon: 80.27 },
    { n: 'Delhi', c: 'India', lat: 28.61, lon: 77.21 },
    { n: 'Kathmandu', c: 'Nepal', lat: 27.72, lon: 85.32 },
    { n: 'Colombo', c: 'Sri Lanka', lat: 6.93, lon: 79.86 },
    { n: 'Beijing', c: 'China', lat: 39.90, lon: 116.41 },
    { n: 'Shanghai', c: 'China', lat: 31.23, lon: 121.47 },
    { n: 'Hong Kong', c: 'China', lat: 22.32, lon: 114.17 },
    { n: 'Taipei', c: 'Taiwan', lat: 25.03, lon: 121.57 },
    { n: 'Seoul', c: 'South Korea', lat: 37.57, lon: 126.98 },
    { n: 'Tokyo', c: 'Japan', lat: 35.68, lon: 139.69 },
    { n: 'Osaka', c: 'Japan', lat: 34.69, lon: 135.50 },
    { n: 'Sydney', c: 'Australia', lat: -33.87, lon: 151.21 },
    { n: 'Auckland', c: 'New Zealand', lat: -36.85, lon: 174.76 },
    { n: 'Honolulu', c: 'USA', lat: 21.31, lon: -157.86 },
    { n: 'Los Angeles', c: 'USA', lat: 34.05, lon: -118.24 },
    { n: 'San Francisco', c: 'USA', lat: 37.77, lon: -122.42 },
    { n: 'Mexico City', c: 'Mexico', lat: 19.43, lon: -99.13 },
    { n: 'Lima', c: 'Peru', lat: -12.05, lon: -77.04 },
    { n: 'Santiago', c: 'Chile', lat: -33.45, lon: -70.67 },
    { n: 'London', c: 'UK', lat: 51.51, lon: -0.13 },
    { n: 'Paris', c: 'France', lat: 48.86, lon: 2.35 },
    { n: 'Istanbul', c: 'Turkey', lat: 41.01, lon: 28.98 },
    { n: 'Dubai', c: 'UAE', lat: 25.20, lon: 55.27 },
    { n: 'Cairo', c: 'Egypt', lat: 30.04, lon: 31.24 },
    { n: 'Nairobi', c: 'Kenya', lat: -1.29, lon: 36.82 }
  ];
  A.gazetteerSearch = function (q) {
    q = String(q || '').trim().toLowerCase();
    if (q.length < 2) return [];
    return A.gazetteer.filter(g => g.n.toLowerCase().indexOf(q) === 0 || g.n.toLowerCase().indexOf(q) > 0).slice(0, 8);
  };
})();
