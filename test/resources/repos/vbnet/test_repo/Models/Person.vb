Namespace TestProject.Models

    Public Class Person

        Public Property Name As String
        Public Property Age As Integer
        Public Property Email As String

        Public Sub New(name As String, age As Integer, email As String)
            Me.Name = name
            Me.Age = age
            Me.Email = email
        End Sub

        Public Function IsAdult() As Boolean
            Return Me.Age >= 18
        End Function

        Public Function CalculateYearsUntilRetirement() As Integer
            Dim calculator As New Calculator()
            Return calculator.Subtract(65, Me.Age)
        End Function

        Public Overrides Function ToString() As String
            Return $"{Me.Name} ({Me.Age}) - {Me.Email}"
        End Function

    End Class

End Namespace
