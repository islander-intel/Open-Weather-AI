import requests
import json
from anthropic import Anthropic, APIError
import logging
import os
from dotenv import load_dotenv 
from geopy.geocoders import Nominatim
import time
# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Load environment variables from tokens.env file
load_dotenv("tokens.env")

# Get Claude API Key from environment variables
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")
if not CLAUDE_API_KEY:
    raise ValueError("Claude API Key not found. Make sure it is set in the tokens.env file.")
claude = Anthropic(api_key=CLAUDE_API_KEY)
cluade_model = "claude-3-haiku-20240307"
# NOAA API Base URL
NOAA_API_BASE_URL = "https://api.weather.gov/points/"
weather_cache = {}
CACHE_EXPIRY = 30 * 60  # 30 minutes in seconds

def cache_weather_data(latitude, longitude, data):
    """Cache weather data with timestamp."""
    cache_key = f"{latitude},{longitude}"
    weather_cache[cache_key] = {
        "data": data,
        "timestamp": time.time()
    }

def get_cached_weather_data(latitude, longitude):
    """Retrieve cached weather data if not expired."""
    cache_key = f"{latitude},{longitude}"
    if cache_key in weather_cache:
        cache_entry = weather_cache[cache_key]
        if time.time() - cache_entry["timestamp"] < CACHE_EXPIRY:
            logging.info(f"Using cached weather data for {cache_key}")
            return cache_entry["data"]
    return None
def get_location_from_claude(user_input):
    """Gets location from Claude API."""
    try:
        response = claude.messages.create(
            model=cluade_model,
            max_tokens=1000,
            temperature=0.0,  # Lower temperature for more deterministic output
            system="You are a location extractor. Your ONLY job is to extract the location from user input. "
                "Return ONLY the location name with no additional text, explanation, or punctuation. "
                "Format examples: 'Newport, RI', 'London', '90210'. "
                "If no location is found, return only the word 'None'. "
                "Do not include any other text in your response.",
            messages=[
                {"role": "user", "content": user_input}
            ]
        )
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        total_tokens = input_tokens + output_tokens
        print(f"Input tokens: {input_tokens}, Output tokens: {output_tokens}, Total: {total_tokens}")
        location_text = response.content[0].text.strip()
        print(f"the location  is {location_text}")
        logging.info(f"Extracted location: {location_text}")
        return location_text if location_text and location_text.lower() != "none" else None
    except APIError as e:
        logging.error(f"Claude API error: {e}")
        return None

def get_coordinates(location_text):
    """Gets the latitude and longitude using geopy."""
    try:
        from geopy.geocoders import Nominatim
        geolocator = Nominatim(user_agent="weather_bot")
        location = geolocator.geocode(location_text)
        if location:
            return location.latitude, location.longitude
        else:
            logging.warning(f"Could not find coordinates for {location_text}")
            return None
    except ImportError:
        logging.error("geopy not installed. Please install it using: pip install geopy")
        return None

def get_weather(latitude, longitude):
    """Retrieves comprehensive weather data from the NWS API with improved error handling."""
    # Check for cached data first
    cached_data = get_cached_weather_data(latitude, longitude)
    if cached_data:
        return cached_data
        
    # Check if location is in US (approximate boundaries)
    us_mainland = (24.0 <= latitude <= 50.0) and (-125.0 <= longitude <= -66.0)
    alaska = (51.0 <= latitude <= 72.0) and (-170.0 <= longitude <= -130.0)
    hawaii = (18.0 <= latitude <= 23.0) and (-161.0 <= longitude <= -154.0)
    
    if not (us_mainland or alaska or hawaii):
        logging.warning(f"Location ({latitude}, {longitude}) appears to be outside the US. NWS API only covers US territories.")
        return {"error": "location_not_in_us"}
    
    url = f"{NOAA_API_BASE_URL}{latitude},{longitude}"
    try:
        response = requests.get(url, headers={'User-Agent': 'WeatherBot/1.0'}, timeout=10)
        response.raise_for_status()
        point_data = response.json()

        properties = point_data['properties']
        grid_id = properties['gridId']
        grid_x = properties['gridX']
        grid_y = properties['gridY']

        # Grid Forecasts
        grid_forecast_url = f"https://api.weather.gov/gridpoints/{grid_id}/{grid_x},{grid_y}/forecast"
        grid_forecast_hourly_url = f"https://api.weather.gov/gridpoints/{grid_id}/{grid_x},{grid_y}/forecast/hourly"
        
        headers = {'User-Agent': 'WeatherBot/1.0'}
        grid_forecast_response = requests.get(grid_forecast_url, headers=headers, timeout=10)
        grid_forecast_hourly_response = requests.get(grid_forecast_hourly_url, headers=headers, timeout=10)
        
        grid_forecast_response.raise_for_status()
        grid_forecast_hourly_response.raise_for_status()
        
        grid_forecast_data = grid_forecast_response.json()
        grid_forecast_hourly_data = grid_forecast_hourly_response.json()

        # Station Observations
        stations_url = f"https://api.weather.gov/points/{latitude},{longitude}/stations"
        stations_response = requests.get(stations_url, headers=headers, timeout=10)
        stations_response.raise_for_status()
        stations_data = stations_response.json()

        observations = []
        if stations_data['features']:
            for station in stations_data['features'][:3]:  # Limit to 3 stations
                station_id = station['properties']['stationIdentifier']
                observations_url = f"https://api.weather.gov/stations/{station_id}/observations/latest"
                observations_response = requests.get(observations_url, headers=headers, timeout=10)
                if observations_response.status_code == 200:
                    observations.append(observations_response.json())

        # Alerts
        alerts_url = f"https://api.weather.gov/alerts/active?point={latitude},{longitude}"
        alerts_response = requests.get(alerts_url, headers=headers, timeout=10)
        alerts_response.raise_for_status()
        alerts_data = alerts_response.json()

        # Build comprehensive data dictionary
        weather_data = {
            "point_data": point_data,
            "grid_forecast": grid_forecast_data,
            "grid_forecast_hourly": grid_forecast_hourly_data,
            "stations": stations_data,
            "observations": observations,
            "alerts": alerts_data,
        }

        # Cache the data before returning
        cache_weather_data(latitude, longitude, weather_data)
        return weather_data

    except requests.exceptions.Timeout:
        logging.error(f"Timeout error fetching weather data for coordinates: {latitude}, {longitude}")
        return {"error": "request_timeout", "details": "The request to the weather service timed out"}
    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching weather data: {e}")
        return {"error": "api_request_failed", "details": str(e)}
    except (KeyError, ValueError, json.JSONDecodeError) as e:
        logging.error(f"Error parsing weather data: {e}")
        return {"error": "data_parsing_failed", "details": str(e)}

def display_weather(weather_data, location_text, user_input):
    """Displays the weather forecast using Claude with optimized prompts and error handling."""
    if not weather_data:
        print("No weather data to display.")
        return
        
    if "error" in weather_data:
        if weather_data["error"] == "location_not_in_us":
            print(f"I'm sorry, but {location_text} appears to be outside the United States. The National Weather Service API only provides data for US locations.")
        elif weather_data["error"] == "request_timeout":
            print("I apologize, but the weather service request timed out. Please try again later.")
        else:
            print(f"I apologize, but I couldn't retrieve the weather data at this time. Error: {weather_data.get('details', 'Unknown error')}")
        return
        
    try:
        # Filter data to reduce token usage
        filtered_data = {
            "location": location_text,
            "alerts": weather_data["alerts"],
            "observations": weather_data["observations"]
        }
        
        # Only include first few periods from forecasts to reduce tokens
        if "properties" in weather_data["grid_forecast"] and "periods" in weather_data["grid_forecast"]["properties"]:
            filtered_data["forecast"] = {
                "periods": weather_data["grid_forecast"]["properties"]["periods"]
            }
            
        if "properties" in weather_data["grid_forecast_hourly"] and "periods" in weather_data["grid_forecast_hourly"]["properties"]:
            filtered_data["hourly_forecast"] = {
                "periods": weather_data["grid_forecast_hourly"]["properties"]["periods"]#[:8]  # Next 8 hours
            }
        
        response = claude.messages.create(
            model=cluade_model,
            max_tokens=1000,
            temperature=0.5,
            system="You are a professional weather service. Provide clear, concise weather information based on the data provided. Include temperature, conditions, precipitation chances, and other relevant weather factors. Start with a direct weather summary.",
            messages=[
                {"role": "user", "content": f"Weather data for {location_text}:\n{json.dumps(filtered_data, indent=2)}\nUser Query: {user_input}\n\nProvide a weather report based on this data."}
            ]
        )
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        total_tokens = input_tokens + output_tokens
        logging.info(f"Weather report - Input tokens: {input_tokens}, Output tokens: {output_tokens}, Total: {total_tokens}")
        analysis = response.content[0].text.strip()
        print(analysis)
    except APIError as e:
        logging.error(f"Claude API error: {e}")
        print("I apologize, but I'm having trouble generating your weather report. Please try again later.")
    except (KeyError, ValueError, json.JSONDecodeError) as e:
        logging.error(f"Error processing weather data: {e}")
        print("I apologize, but there was an issue processing the weather data. Please try again.")
def main():
    """Main function to run the Weather Bot."""
    print("Welcome to the Weather Bot! Ask me about the weather for any location.")
    
    while True:
        user_input = input("Enter your query (or type 'exit' to quit): ").strip()
        
        if user_input.lower() == 'exit':
            print("Thank you for using Weather Bot!")
            break
        
        location_text = get_location_from_claude(user_input)
        
        if location_text:
            coordinates = get_coordinates(location_text)
            
            if coordinates:
                latitude, longitude = coordinates
                weather_data = get_weather(latitude, longitude)
                display_weather(weather_data, location_text, user_input)

if __name__ == "__main__":
    main()
