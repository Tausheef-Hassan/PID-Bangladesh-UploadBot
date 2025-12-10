local p = {}

-- Check if string ends with an image file extension
local function hasImageExtension(str)
    return str:match("%.[a-zA-Z][a-zA-Z][a-zA-Z]?[a-zA-Z]?$") ~= nil
end

-- Normalize URL for comparison: remove protocol and handle domain variations
local function normalizeURLForMatching(url)
    if not url then return nil end
    
    local normalized = url:gsub("^https?://", "")
    normalized = normalized:gsub("^pressinform%.portal%.gov%.bd/", "pressinform.gov.bd/")
    
    return normalized
end

-- Convert database date format to ISO format (handle AM/PM and standard dates)
local function normalizeDate(dateStr)
    if not dateStr then return nil end
    
    -- Try to parse date with time and AM/PM (with seconds)
    local year, month, day, hour, minute, second, ampm = dateStr:match("^(%d%d%d%d)%-(%d%d)%-(%d%d)%s+(%d%d):(%d%d):(%d%d)%s*([ap]m)")
    
    if year and month and day and hour and minute and ampm then
        local hourNum = tonumber(hour)
        if ampm == "pm" and hourNum ~= 12 then
            hourNum = hourNum + 12
        elseif ampm == "am" and hourNum == 12 then
            hourNum = 0
        end
        return string.format("%s-%s-%s %02d:%s:%s", year, month, day, hourNum, minute, second)
    end
    
    -- Try to parse date with time and AM/PM (without seconds)
    year, month, day, hour, minute, ampm = dateStr:match("^(%d%d%d%d)%-(%d%d)%-(%d%d)%s+(%d%d):(%d%d)%s*([ap]m)")
    
    if year and month and day and hour and minute and ampm then
        local hourNum = tonumber(hour)
        if ampm == "pm" and hourNum ~= 12 then
            hourNum = hourNum + 12
        elseif ampm == "am" and hourNum == 12 then
            hourNum = 0
        end
        return string.format("%s-%s-%s %02d:%s", year, month, day, hourNum, minute)
    end
    
    -- Try to parse date with 24-hour time (with seconds)
    year, month, day, hour, minute, second = dateStr:match("^(%d%d%d%d)%-(%d%d)%-(%d%d)%s+(%d%d):(%d%d):(%d%d)")
    if year and month and day and hour and minute and second then
        return string.format("%s-%s-%s %s:%s:%s", year, month, day, hour, minute, second)
    end
    
    -- Try to parse date with 24-hour time (without seconds)
    year, month, day, hour, minute = dateStr:match("^(%d%d%d%d)%-(%d%d)%-(%d%d)%s+(%d%d):(%d%d)")
    if year and month and day and hour and minute then
        return string.format("%s-%s-%s %s:%s", year, month, day, hour, minute)
    end
    
    -- Try to parse date only (no time)
    year, month, day = dateStr:match("^(%d%d%d%d)%-(%d%d)%-(%d%d)")
    if year and month and day then
        return year .. '-' .. month .. '-' .. day
    end
    
    return nil
end

-- Cache for date data to avoid reloading
local dateDataCache

-- Load date data from all yearly submodules
local function getDateData()
    if dateDataCache then return dateDataCache end
    
    local allData = {}
    local currentYear = tonumber(os.date("%Y"))
    local endYear = math.max(currentYear + 1, 2025)
    
    -- Load data from Module:PIDDateData/2015, /2016, /2017, etc.
    for year = 2015, endYear do
        local success, yearData = pcall(mw.loadData, 'Module:PIDDateData/' .. tostring(year))
        if success and yearData then
            for url, date in pairs(yearData) do
                allData[url] = date
            end
        end
    end
    
    dateDataCache = allData
    return allData
end

-- Clean URL: normalize spaces and remove tracking parameters
local function processURL(url)
    if not url or url == '' then return nil end
    
    url = mw.text.trim(url)
    
    -- Remove URL tracking parameters (anything after the file extension)
    url = url:gsub("(%.[a-zA-Z][a-zA-Z][a-zA-Z]?[a-zA-Z]?)[%?&].*$", "%1")
    
    -- Convert spaces to %20 if not part of image filename
    local spacePos = url:find(' ')
    if spacePos then
        local beforeSpace = url:sub(1, spacePos - 1)
        if not hasImageExtension(beforeSpace) then
            url = url:gsub(' ', '%%20')
        end
    end
    
    return url
end

-- Find matching date in database (handles space/%20 variations and normalization)
local function findDateInDatabase(url, dateData)
    if not url then return nil, false end
    
    local function extractDateAndHistoric(dbEntry)
        if type(dbEntry) == "string" then
            return dbEntry, false
        elseif type(dbEntry) == "table" then
            return dbEntry.date, dbEntry.historic or false
        end
        return nil, false
    end
    
    -- Direct match first
    if dateData[url] then
        return extractDateAndHistoric(dateData[url])
    end
    
    -- Try with space variation (convert %20 to space for matching with dataset)
    local urlWithSpaces = url:gsub("%%20", " ")
    if dateData[urlWithSpaces] then
        return extractDateAndHistoric(dateData[urlWithSpaces])
    end
    
    -- Try with normalized protocol/domain
    local normalizedURL = normalizeURLForMatching(url)
    local normalizedURLWithSpaces = normalizeURLForMatching(urlWithSpaces)
    
    for dbURL, dbEntry in pairs(dateData) do
        local normalizedDB = normalizeURLForMatching(dbURL)
        -- Check both %20 and space versions
        if normalizedURL == normalizedDB or normalizedURLWithSpaces == normalizedDB then
            return extractDateAndHistoric(dbEntry)
        end
    end
    
    return nil, false
end

-- Extract Source-PID URL from page wikitext
local function getSourcePIDURL(content)
    if not content then return nil end
    
    -- Match {{Source-PID|url=...}} or {{Source-PID|...}}
    local url = content:match('%{%{%s*[Ss][Oo][Uu][Rr][Cc][Ee]%-[Pp][Ii][Dd]%s*|%s*url%s*=%s*([^}|]+)')
    if not url then
        url = content:match('%{%{%s*[Ss][Oo][Uu][Rr][Cc][Ee]%-[Pp][Ii][Dd]%s*|%s*([^}|]+)')
    end
    
    return url and mw.text.trim(url) or nil
end

-- Main function: Add categories based on date information
-- Called from Template:PD-BDGov-PID
function p.checkCategory(frame)
    local page = mw.title.getCurrentTitle()
    local content = page:getContent()
    local result = ''

    if not content then
        return '[[Category:Press Information Department images|' .. page.text .. ']]'
    end

    -- Check if page already has {{Date-PID}} template
    local contentLower = string.lower(content)
    local hasDatePID = contentLower:find('%{%{%s*date[%s%-]pid')
    
    -- Extract URL from Source-PID template in page wikitext
    local url = getSourcePIDURL(content)
    url = processURL(url)
    
    local dbDateNormalized = nil
    
    -- Look up date from database if URL exists
    if url and url ~= '' then
        local dateData = getDateData()
        local dbDate, isHistoric = findDateInDatabase(url, dateData)
        p._isHistoric = isHistoric
        if dbDate then
            dbDateNormalized = normalizeDate(dbDate)
        end
    end
    
    -- Add date categories if no Date-PID template but date found in database
    if not hasDatePID and dbDateNormalized then
        local dbYear, dbMonth, dbDay = dbDateNormalized:match("^(%d%d%d%d)%-(%d%d)%-(%d%d)")
        if dbYear and dbMonth and dbDay then
            -- Only add date categories if not marked as historic
            if not p._isHistoric then
                local monthName = os.date("%B %Y", os.time{year=tonumber(dbYear), month=tonumber(dbMonth), day=1})
                result = result .. '[[Category:PID-BD images from ' .. monthName .. ']]'
                result = result .. '[[Category:Bangladesh photographs taken on ' .. dbYear .. '-' .. dbMonth .. '-' .. dbDay .. ']]'
            else
                result = result .. '[[Category:Historic images from PID-BD]]'
            end
        end
    elseif hasDatePID and p._isHistoric then
        -- If Date-PID template exists but image is historic, add historic category
        result = result .. '[[Category:Historic images from PID-BD]]'
    elseif not hasDatePID then
        -- Fallback category if no date found
        result = '[[Category:Press Information Department images|' .. page.text .. ']]'
    end

    -- Check if page has meaningful categories (not just "Uploaded with...")
    local hasCategory = false
    for category in content:gmatch('%[%[Category:([^%]]+)%]%]') do
        if not string.lower(category):match('^%s*uploaded%s+with') then
            hasCategory = true
            break
        end
    end

    if not hasCategory then
        result = result .. '{{Uncategorized PID Image}}'
    end

    return frame:preprocess(result)
end

-- Override manual date with database date if available
-- Called from Template:Date-PID
function p.checkOverride(frame)
    local manualDate = frame.args[1] or ''
    
    -- Extract URL from Source-PID template in page wikitext
    local page = mw.title.getCurrentTitle()
    local content = page:getContent()
    local url = getSourcePIDURL(content)
    url = processURL(url)
    
    if not url or url == '' then
        return manualDate
    end
    
    local dateData = getDateData()
    local dbDate, isHistoric = findDateInDatabase(url, dateData)
    
    -- Store historic flag for other functions to access
    p._isHistoric = isHistoric
    
    if dbDate then
        local isoDate = normalizeDate(dbDate)
        if isoDate then
            return isoDate
        end
    end
    
    return manualDate
end

-- Process URL: clean and normalize for external use
function p.processURL(frame)
    local url = frame.args[1] or frame.args.url or ''
    if url == '' then return '' end
    return processURL(url)
end

-- Access specific year's data
function p.getData(frame)
    local year = frame.args[1] or frame.args.year
    if not year then return {} end
    
    local success, data = pcall(mw.loadData, 'Module:PIDDateData/' .. year)
    if success then return data end
    return {}
end

-- Access all combined date data
function p.getAllData(frame)
    return getDateData()
end

-- Check if image should get date categories (returns empty string for historic images)
function p.shouldAddDateCategories(frame)
    local page = mw.title.getCurrentTitle()
    local content = page:getContent()
    
    if not content then return "yes" end
    
    local url = getSourcePIDURL(content)
    url = processURL(url)
    
    if not url or url == '' then return "yes" end
    
    local dateData = getDateData()
    local dbDate, isHistoric = findDateInDatabase(url, dateData)
    
    -- Store historic flag for consistency
    p._isHistoric = isHistoric
    
    -- Return empty string if historic (for {{#if:}} to treat as false)
    -- Return "yes" if not historic (for {{#if:}} to treat as true)
    if isHistoric then
        return ""
    else
        return "yes"
    end
end

return p
