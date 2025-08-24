// energy-prices-downloader.js
const fs = require('fs');
const https = require('https');
const url = require('url');

const PSE_API_BASE_URL = 'https://api.raporty.pse.pl/api';

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

async function makeProxyRequest(apiPath) {
    return new Promise((resolve, reject) => {
        const targetUrl = `${PSE_API_BASE_URL}${apiPath}`;
        const parsedUrl = url.parse(targetUrl);

        const options = {
            hostname: parsedUrl.hostname,
            port: parsedUrl.port || 443,
            path: parsedUrl.path,
            method: 'GET',
            headers: {
                'Accept': 'application/json',
                'Content-Type': 'application/json',
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                'Accept-Language': 'en-US,en;q=0.9',
                'Accept-Encoding': 'gzip, deflate, br',
                'Connection': 'keep-alive'
            }
        };

        const req = https.request(options, (res) => {
            let data = '';

            res.on('data', (chunk) => {
                data += chunk;
            });

            res.on('end', () => {
                if (res.statusCode === 200) {
                    try {
                        const jsonData = JSON.parse(data);
                        
                        // Rewrite nextLink to be relative to our API base
                        if (jsonData.nextLink && typeof jsonData.nextLink === 'string') {
                            const nextUrl = new URL(jsonData.nextLink);
                            jsonData.nextLink = nextUrl.pathname.replace('/api', '') + (nextUrl.search || '');
                        }
                        
                        resolve(jsonData);
                    } catch (error) {
                        reject(new Error(`Failed to parse JSON: ${error.message}`));
                    }
                } else {
                    reject(new Error(`HTTP error! Status: ${res.statusCode}`));
                }
            });
        });

        req.on('error', (error) => {
            reject(new Error(`Request failed: ${error.message}`));
        });

        req.end();
    });
}

async function fetchWithRetry(apiPath, retries = 0) {
    const maxRetries = 3;
    const baseDelay = 1000;

    try {
        console.log(`Fetching: ${apiPath}`);
        const response = await makeProxyRequest(apiPath);
        return response;
    } catch (error) {
        if (retries < maxRetries) {
            const delay = baseDelay * Math.pow(2, retries);
            console.log(`Request failed, retrying in ${delay}ms... (${retries + 1}/${maxRetries})`);
            await sleep(delay);
            return fetchWithRetry(apiPath, retries + 1);
        } else {
            throw error;
        }
    }
}

async function fetchAllData(initialApiPath) {
    let allData = [];
    let nextUrl = initialApiPath;
    let pageCount = 0;

    while (nextUrl) {
        pageCount++;
        console.log(`Fetching page ${pageCount}...`);
        
        try {
            const response = await fetchWithRetry(nextUrl);
            
            if (response.value && Array.isArray(response.value)) {
                allData = allData.concat(response.value);
                console.log(`Page ${pageCount}: Got ${response.value.length} records. Total so far: ${allData.length}`);
            } else {
                console.log(`Page ${pageCount}: No 'value' array found in response`);
                break;
            }

            // Check for nextLink
            if (response.nextLink) {
                nextUrl = response.nextLink;
                console.log(`Next page available: ${nextUrl}`);
                await sleep(100); // Small delay between requests
            } else {
                console.log('No more pages available');
                nextUrl = null;
            }
        } catch (error) {
            console.error(`Error fetching page ${pageCount}:`, error.message);
            throw error;
        }
    }

    return allData;
}

async function fetchDataset(endpoint, outputFile, startDate, endDate) {
    console.log(`\n=== Fetching ${endpoint} ===`);
    const apiPath = `/${endpoint}?$first=1000&$filter=dtime%20ge%20'${startDate}'%20and%20dtime%20le%20'${endDate}'`;
    const allData = await fetchAllData(apiPath);
    
    if (allData.length > 0) {
        fs.writeFileSync(outputFile, JSON.stringify(allData, null, 2));
        
        console.log(`Download completed successfully!`);
        console.log(`Total records downloaded: ${allData.length}`);
        console.log(`Data saved to: ${outputFile}`);
        
        // Show data range
        if (allData.length > 0) {
            const firstRecord = allData[0];
            const lastRecord = allData[allData.length - 1];
            console.log(`Data range: ${firstRecord.dtime} to ${lastRecord.dtime}`);
        }
        
        return allData.length;
    } else {
        console.log('No data was downloaded');
        return 0;
    }
}

async function main() {
    const startDate = '2024-07-01';
    const endDate = '2025-06-30';
    
    console.log(`Fetching PSE data from ${startDate} to ${endDate}`);
    
    // Create output directory
    if (!fs.existsSync('pse_data_js')) {
        fs.mkdirSync('pse_data_js');
    }
    
    let totalRecords = 0;
    
    try {
        // Fetch the four required datasets with pagination
        console.log('Fetching all required PSE datasets...');
        
        // 1. Energy/balancing prices (CEB średnia)
        totalRecords += await fetchDataset('energy-prices', 'pse_data_js/energy_prices.json', startDate, endDate);
        
        // 2. aFRR volumes (MBP-TP afrr_d field)
        totalRecords += await fetchDataset('mbp-tp', 'pse_data_js/afrr_volumes_mbp.json', startDate, endDate);
        
        // 3. Total costs (KMB-KRO-ROZL)
        totalRecords += await fetchDataset('kmb-kro-rozl', 'pse_data_js/total_costs.json', startDate, endDate);
        
        // 4. aFRR marginal prices (CMBU-TU)
        totalRecords += await fetchDataset('cmbu-tu', 'pse_data_js/afrr_marginal_prices.json', startDate, endDate);
        
        console.log(`\n=== SUMMARY ===`);
        console.log(`Total records downloaded: ${totalRecords}`);
        console.log('All PSE datasets downloaded with full pagination handling');
        
    } catch (error) {
        console.error('Error during data fetch:', error.message);
    }
}

main().catch(console.error);
