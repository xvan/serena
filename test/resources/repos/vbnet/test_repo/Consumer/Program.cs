using TestProject.Models;

namespace Consumer;

internal static class Program
{
    private static void Main()
    {
        var person = new Person("Ada", 36, "ada@example.com");
        System.Console.WriteLine(person.Name);
        System.Console.WriteLine(person.IsAdult());
        System.Console.WriteLine(person.CalculateYearsUntilRetirement());
    }
}